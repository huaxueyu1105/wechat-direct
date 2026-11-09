"""Persistent, account-bound schedules; callbacks own all actual sending.

Missed once jobs run once at the next check. Repeating jobs run only their latest
due occurrence, never a backlog. DST gaps are skipped; repeated local times use
their first occurrence. Pausing/cancelling cannot withdraw an already claimed
send. Unknown outcomes are queried with the original key and never resubmitted.
"""
import copy
from datetime import datetime, time as wall_time, timedelta, timezone as dt_timezone
import json
import math
import re
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TERMINAL = ('confirmed', 'not_submitted', 'closed')
SCENES = ('casual', 'greeting', 'thanks', 'celebration', 'formal', 'serious', 'conflict', 'condolence')


class ScheduleError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, code, message):
    if not condition:
        raise ScheduleError(code, message)


def zone(name):
    require(isinstance(name, str) and bool(name), 'INVALID_TIMEZONE', '请提供 IANA 时区名称。')
    if name == 'Asia/Shanghai':
        return dt_timezone(timedelta(hours=8), name)
    if name == 'UTC':
        return dt_timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ScheduleError('TIMEZONE_UNAVAILABLE', '时区不可用；请安装该 IANA 时区数据，或使用 Asia/Shanghai / UTC。') from error


def timestamp(now=None):
    value = time.time() if now is None else now
    if isinstance(value, datetime):
        require(value.tzinfo is not None and value.utcoffset() is not None,
                'INVALID_SCHEDULE_TIME', '时间必须包含时区。')
        value = value.timestamp()
    require(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
            'INVALID_SCHEDULE_TIME', '时间必须是有效时间戳或含时区的日期。')
    return float(value)


def iso(value):
    return datetime.fromtimestamp(value, dt_timezone.utc).isoformat() if value is not None else None


def validate_payload(payload):
    require(isinstance(payload, dict), 'INVALID_SCHEDULE_PAYLOAD', '定时消息内容必须为对象。')
    fields = {
        'send': {'action', 'recipientId', 'text'},
        'send-sticker': {'action', 'recipientId', 'stickerId', 'scene', 'mode'},
        'send-image': {'action', 'recipientId', 'filePath', 'fileSha256'},
        'send-file': {'action', 'recipientId', 'filePath', 'fileSha256'},
    }
    action = payload.get('action')
    require(isinstance(action, str) and action in fields and set(payload) <= fields[action],
            'INVALID_SCHEDULE_PAYLOAD', '只支持固定目标的文字、表情、图片或文件发送。')
    def nonempty(key, maximum):
        value = payload.get(key)
        require(isinstance(value, str) and bool(value.strip()) and '\0' not in value and len(value.encode('utf8')) <= maximum,
                'INVALID_SCHEDULE_PAYLOAD', key + ' 不能为空、含空字符或超过长度限制。')
    nonempty('recipientId', 256)
    if action == 'send':
        nonempty('text', 16000)
    elif action == 'send-sticker':
        nonempty('stickerId', 200)
        require(payload.get('scene', 'serious') in SCENES and payload.get('mode', 'contextual') in ('contextual', 'explicit'),
                'INVALID_SCHEDULE_PAYLOAD', '表情场景或发送模式无效。')
    else:
        nonempty('filePath', 32768)
        if 'fileSha256' in payload:
            require(isinstance(payload['fileSha256'], str) and bool(re.fullmatch('[0-9a-fA-F]{64}', payload['fileSha256'])),
                    'INVALID_SCHEDULE_PAYLOAD', '文件摘要必须为 SHA-256。')
    return copy.deepcopy(payload)


def validate_schedule(schedule):
    require(isinstance(schedule, dict), 'INVALID_SCHEDULE', '定时规则必须为对象。')
    kind = schedule.get('kind')
    if kind == 'once':
        require(set(schedule) == {'kind', 'at'} and isinstance(schedule.get('at'), str),
                'INVALID_SCHEDULE', '一次性任务需要 kind=once 及含时区的 at。')
        try:
            date = datetime.fromisoformat(schedule['at'].replace('Z', '+00:00'))
        except ValueError as error:
            raise ScheduleError('INVALID_SCHEDULE', 'at 必须是含时区的 ISO 日期时间。') from error
        require(date.tzinfo is not None and date.utcoffset() is not None, 'INVALID_SCHEDULE', 'at 必须明确包含时区。')
        return {'kind': kind, 'at': date.astimezone(dt_timezone.utc).isoformat()}
    require(kind in ('daily', 'weekly'), 'INVALID_SCHEDULE', 'kind 需为 once、daily 或 weekly。')
    expected = {'kind', 'time'} | ({'weekdays'} if kind == 'weekly' else set())
    require(set(schedule) == expected and isinstance(schedule.get('time'), str) and
            bool(re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', schedule['time'])),
            'INVALID_SCHEDULE', '循环任务需要 HH:MM 格式的 time。')
    result = {'kind': kind, 'time': schedule['time']}
    if kind == 'weekly':
        days = schedule['weekdays']
        require(isinstance(days, list) and bool(days) and len(days) <= 7 and
                all(isinstance(day, int) and not isinstance(day, bool) and 0 <= day <= 6 for day in days),
                'INVALID_SCHEDULE', 'weekdays 需为 0–6 的列表，周一为 0。')
        result['weekdays'] = sorted(set(days))
    return result


def local_occurrence(date, schedule, tz):
    if schedule['kind'] == 'weekly' and date.weekday() not in schedule['weekdays']:
        return None
    local = datetime.combine(date, wall_time.fromisoformat(schedule['time']))
    aware = local.replace(tzinfo=tz, fold=0)
    if aware.astimezone(dt_timezone.utc).astimezone(tz).replace(tzinfo=None) != local:
        return None  # The clock jumps over this local time.
    return aware.timestamp()


def occurrence(schedule, tz, value, latest=False, inclusive=False):
    if schedule['kind'] == 'once':
        return datetime.fromisoformat(schedule['at']).timestamp()
    date = datetime.fromtimestamp(value, tz).date()
    for offset in range(15):
        candidate = local_occurrence(date + timedelta(days=-offset if latest else offset), schedule, tz)
        if candidate is not None and (candidate <= value if latest else candidate > value or (inclusive and candidate == value)):
            return candidate
    raise ScheduleError('SCHEDULE_TIME_UNAVAILABLE', '无法计算下一次有效执行时间。')


class Scheduler:
    def __init__(self, path, account_id):
        require(isinstance(account_id, str) and bool(account_id.strip()), 'ACCOUNT_NOT_BOUND', '定时任务必须绑定账号。')
        self.account_id = account_id
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS schedule_tasks ('
                        'id TEXT PRIMARY KEY, account TEXT NOT NULL, payload TEXT NOT NULL, rule TEXT NOT NULL, '
                        'timezone TEXT NOT NULL, state TEXT NOT NULL, next_run REAL, created REAL NOT NULL, updated REAL NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS schedule_runs ('
                        'id TEXT PRIMARY KEY, task_id TEXT NOT NULL, scheduled_at REAL NOT NULL, operation_key TEXT NOT NULL UNIQUE, '
                        'state TEXT NOT NULL, result TEXT NOT NULL, updated REAL NOT NULL, UNIQUE(task_id, scheduled_at))')
        self.db.execute('CREATE INDEX IF NOT EXISTS schedule_runs_task ON schedule_runs(task_id, scheduled_at)')
        self.db.commit()

    def close(self):
        self.db.close()

    def _task(self, task_id):
        row = self.db.execute('SELECT * FROM schedule_tasks WHERE id=? AND account=?', (task_id, self.account_id)).fetchone()
        require(row is not None, 'SCHEDULE_NOT_FOUND', '没有当前账号的此定时任务。')
        return row

    @staticmethod
    def _public_run(row):
        return {'runId': row['id'], 'scheduledAt': iso(row['scheduled_at']), 'operationKey': row['operation_key'],
                'status': row['state'], 'result': json.loads(row['result']), 'updatedAt': iso(row['updated'])}

    def get(self, task_id):
        row = self._task(task_id)
        last = self.db.execute('SELECT * FROM schedule_runs WHERE task_id=? ORDER BY scheduled_at DESC LIMIT 1', (task_id,)).fetchone()
        return {'taskId': row['id'], 'accountId': row['account'], 'payload': json.loads(row['payload']),
                'schedule': json.loads(row['rule']), 'timezone': row['timezone'], 'status': row['state'],
                'nextRunAt': iso(row['next_run']), 'createdAt': iso(row['created']), 'updatedAt': iso(row['updated']),
                'missedRunPolicy': 'latest', 'lastRun': self._public_run(last) if last else None}

    def list(self, include_inactive=True):
        query = 'SELECT id FROM schedule_tasks WHERE account=?' + ('' if include_inactive else " AND state='active'")
        return [self.get(row['id']) for row in self.db.execute(query + ' ORDER BY created,id', (self.account_id,)).fetchall()]

    def create(self, payload, schedule, timezone='Asia/Shanghai', now=None, task_id=None):
        payload = validate_payload(payload)
        schedule = validate_schedule(schedule)
        tz = zone(timezone)
        now = timestamp(now)
        if task_id is None:
            task_id = str(uuid.uuid4())
        else:
            try:
                require(isinstance(task_id, str), 'INVALID_SCHEDULE_ID', 'taskId 必须为固定 UUID。')
                task_id = str(uuid.UUID(task_id))
            except (ValueError, AttributeError) as error:
                raise ScheduleError('INVALID_SCHEDULE_ID', 'taskId 必须为固定 UUID。') from error
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute('SELECT * FROM schedule_tasks WHERE id=?', (task_id,)).fetchone()
            if old is not None:
                require(old['account'] == self.account_id and json.loads(old['payload']) == payload and
                        json.loads(old['rule']) == schedule and old['timezone'] == timezone,
                        'SCHEDULE_CONFLICT', '同一 taskId 的账号、消息或定时规则发生变化，未创建新任务。')
            else:
                next_run = occurrence(schedule, tz, now, inclusive=True)
                self.db.execute('INSERT INTO schedule_tasks VALUES (?,?,?,?,?,?,?,?,?)',
                                (task_id, self.account_id, json.dumps(payload, ensure_ascii=False), json.dumps(schedule), timezone,
                                 'active', next_run, now, now))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self.get(task_id)

    def _set_state(self, task_id, state):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self._task(task_id)
            require(row['state'] in ('active', 'paused') or row['state'] == state,
                    'SCHEDULE_CLOSED', '已完成、失败或取消的任务不能恢复；需要时新建任务。')
            self.db.execute('UPDATE schedule_tasks SET state=?,updated=? WHERE id=?', (state, time.time(), task_id))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self.get(task_id)

    def pause(self, task_id):
        return self._set_state(task_id, 'paused')

    def resume(self, task_id):
        return self._set_state(task_id, 'active')

    def cancel(self, task_id):
        return self._set_state(task_id, 'cancelled')

    def close_run(self, task_id, reviewed=False, result=None):
        require(reviewed is True, 'REVIEW_REQUIRED', '必须先人工核对本次发送结果。')
        task=self.get(task_id)
        run=task['lastRun']
        require(run is not None, 'SCHEDULE_RUN_NOT_FOUND', '此任务尚无执行记录。')
        if run['status'] not in TERMINAL:
            self._finish(run['runId'], result if result is not None else
                         {'status':'closed','operationKey':run['operationKey'],
                          'error':'MANUALLY_CLOSED_OUTCOME_UNKNOWN'}, time.time())
        return self.get(task_id)

    def _finish(self, run_id, result, now):
        if not isinstance(result, dict):
            result = {'status': 'unknown', 'error': 'INVALID_SEND_RESULT'}
        else:
            result = copy.deepcopy(result)
        state = result.get('status')
        if state not in (*TERMINAL, 'submitted', 'unknown'):
            state = 'unknown'
            result['status'] = state
        try:
            encoded = json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            state, encoded = 'unknown', '{"status":"unknown","error":"INVALID_SEND_RESULT"}'
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT * FROM schedule_runs WHERE id=?', (run_id,)).fetchone()
            if row['state'] not in TERMINAL:
                self.db.execute('UPDATE schedule_runs SET state=?,result=?,updated=? WHERE id=?', (state, encoded, now, run_id))
                task = self._task(row['task_id'])
                if state in TERMINAL and json.loads(task['rule'])['kind'] == 'once' and task['state'] in ('active', 'paused'):
                    self.db.execute('UPDATE schedule_tasks SET state=?,updated=? WHERE id=?',
                                    ({'confirmed':'completed','not_submitted':'failed','closed':'cancelled'}[state], now, task['id']))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self._public_run(self.db.execute('SELECT * FROM schedule_runs WHERE id=?', (run_id,)).fetchone())

    @staticmethod
    def _call(callback, *args):
        try:
            return callback(*args)
        except Exception as error:
            return {'status': 'unknown', 'error': getattr(error, 'code', type(error).__name__)}

    def _claim(self, now):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            task = self.db.execute("SELECT t.* FROM schedule_tasks t WHERE t.account=? AND t.state='active' "
                                   "AND t.next_run<=? AND NOT EXISTS (SELECT 1 FROM schedule_runs r WHERE r.task_id=t.id "
                                   "AND r.state NOT IN ('confirmed','not_submitted','closed')) ORDER BY t.next_run,t.id LIMIT 1",
                                   (self.account_id, now)).fetchone()
            if task is None:
                self.db.commit()
                return None
            rule, tz = json.loads(task['rule']), zone(task['timezone'])
            scheduled = occurrence(rule, tz, now, latest=True)
            run_id = str(uuid.uuid4())
            operation_key = 'schedule:' + task['id'] + ':' + str(int(scheduled * 1000000))
            next_run = None if rule['kind'] == 'once' else occurrence(rule, tz, now)
            self.db.execute('INSERT INTO schedule_runs VALUES (?,?,?,?,?,?,?)',
                            (run_id, task['id'], scheduled, operation_key, 'submitting', '{}', now))
            self.db.execute('UPDATE schedule_tasks SET next_run=?,updated=? WHERE id=?', (next_run, now, task['id']))
            self.db.commit()
            return {'runId': run_id, 'taskId': task['id'], 'operationKey': operation_key, 'payload': json.loads(task['payload'])}
        except Exception:
            self.db.rollback()
            raise

    def run_due(self, runner, status_reader=None, now=None, limit=20):
        require(callable(runner) and (status_reader is None or callable(status_reader)), 'INVALID_CALLBACK', '发送与状态查询必须为可调用对象。')
        require(isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 100,
                'INVALID_SCHEDULE_LIMIT', '每次执行上限需为 1–100。')
        now = timestamp(now)
        results = []
        pending = self.db.execute("SELECT r.* FROM schedule_runs r JOIN schedule_tasks t ON t.id=r.task_id "
                                  "WHERE t.account=? AND r.state NOT IN ('confirmed','not_submitted','closed') "
                                  'ORDER BY r.updated,r.scheduled_at LIMIT ?', (self.account_id, limit)).fetchall()
        if status_reader is not None:
            for row in pending:
                result = self._finish(row['id'], self._call(status_reader, row['operation_key']), now)
                results.append({'taskId': row['task_id'], 'action': 'queried', **result})
        else:
            results.extend({'taskId': row['task_id'], 'action': 'unresolved', **self._public_run(row)} for row in pending)
        for _ in range(limit):
            claimed = self._claim(now)
            if claimed is None:
                break
            result = self._finish(claimed['runId'], self._call(runner, claimed['payload'], claimed['operationKey']), now)
            results.append({'taskId': claimed['taskId'], 'action': 'attempted', **result})
        return {'accountId': self.account_id, 'checkedAt': iso(now), 'runs': results, 'missedRunPolicy': 'latest'}
