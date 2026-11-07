"""代拟回复的图片表情策略；纯本地决策，不读取聊天或发送消息。"""
import re
import json
from pathlib import Path

SCENES = ('casual', 'greeting', 'thanks', 'celebration', 'formal', 'serious', 'conflict', 'condolence')
ELIGIBLE_SCENES = ('casual', 'greeting', 'thanks', 'celebration')
WINDOW = 20
SPACING = 5
# 识别最近操作中的常用内置表情、Unicode emoji；用户原文也计入历史。
EXPRESSION = re.compile(r'\[[^\]\r\n]{1,12}\]|[\U0001f000-\U0001faff\u2600-\u27bf\ufe0f\u20e3]')
CLASSIC_CODES=tuple(x['text'] for x in json.loads((Path(__file__).resolve().parents[1]/'runtime/builtin-expressions.json').read_text(encoding='utf8'))['items'] if x['text'].startswith('/:'))


def contains_expression(body):
    return bool(EXPRESSION.search(body)) or any(code in body for code in CLASSIC_CODES)


class ReplyPolicyError(ValueError):
    code = 'INVALID_REPLY'


def policy_info():
    return {
        'mode': 'occasional', 'maxRatio': 0.2, 'rollingWindow': WINDOW,
        'minimumPlainRepliesBetweenExpressions': SPACING - 1, 'maxExpressionsPerReply': 1,
        'eligibleScenes': list(ELIGIBLE_SCENES),
        'plainTextScenes': [x for x in SCENES if x not in ELIGIBLE_SCENES],
        'preferredExpressionType':'contextual_selection', 'imageStickersSupported':True,'animatedStickersSupported':True,
        'ownCreationsOnly':False,'allowedExpressionSources':['wechat_builtin','wechat_favorite'],
        'builtinExpressionsSupported':True,'externalStickerFallback':False,'automaticBuiltinExpressions':False,
        'stickerMaxEdge':320,'stickerMaxBytes':2*1024*1024,
        'automaticReplies': False, 'verbatimMessagesUnchanged': True,
    }


def decision(scene, history):
    if scene not in SCENES:
        raise ReplyPolicyError('scene 需为 casual、greeting、thanks、celebration、formal、serious、conflict 或 condolence。')
    recent = history[:WINDOW - 1]  # 最新在前；新回复连同最近十九条组成窗口。
    has_expression=lambda x:x.get('kind') in ('sticker','builtin') or contains_expression(x.get('text',''))
    used = sum(has_expression(x) for x in recent)
    allowed = scene in ELIGIBLE_SCENES
    reason = 'eligible' if allowed else 'context_not_suitable'
    if allowed and any(has_expression(x) for x in recent[:SPACING - 1]):
        allowed, reason = False, 'spacing_limit'
    if allowed and (used + 1) * SPACING > len(recent) + 1:
        allowed, reason = False, 'ratio_limit'
    return {
        'scene': scene, 'useExpression': allowed, 'reason': reason,
        'expressionType':'contextual_selection','expression':'',
        'recentMessageCount': len(recent), 'recentExpressionCount': used,
        'maxRatio': 0.2, 'rollingWindow': WINDOW,
    }


def compose(body, scene, history):
    if contains_expression(body):
        raise ReplyPolicyError('代拟文字请勿预先添加表情；图片表情使用 send_sticker，用户指定的完整正文请用 send_text 原样发送。')
    result = decision(scene, history)
    result.update(stickerEligible=result['useExpression'],useExpression=False)
    return body,result
