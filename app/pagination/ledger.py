from app.problem import MESSAGES
from .spans import normalize, pages_in, split_limit

# 订单生命周期状态：active 在册有效 / cancelled 已撤销 / restored 已更正恢复（含部分恢复）
ACTIVE, CANCELLED, RESTORED = 'active', 'cancelled', 'restored'


def cancellation(row, active, archive, state, balances):
    target = active.get(row['target'])
    code = row['ruling']
    if not code and target is None:
        phase = state.get(row['target'])
        if phase == CANCELLED:
            code = 'REPEATED_CANCEL'
        elif phase == RESTORED:
            code = 'RESTORED_TARGET'
        else:
            code = 'UNKNOWN_TARGET'
    elif not code and state.get(row['target']) == RESTORED:
        code = 'RESTORED_TARGET'
    if not code and row['org']['code'] != target['org']:
        code = 'WRONG_ORG'
    if not code and row['received'][:4] != target['received'][:4]:
        code = 'BOUNDARY'
    credit = 0
    if not code:
        reference = active.pop(row['target'])
        archive[row['target']] = reference
        state[row['target']] = CANCELLED
        credit = reference['licensed']
        if reference['pool']:
            balances[reference['pool']]['used'] -= credit
    return dict(entry=row['entry'], line=row['line'], target=row['target'], restored=credit, reason=code,
                target_line=target['line'] if target else None)


def restoration(row, target, active, archive, state, balances):
    """误撤更正：只能按当前可用额度逐页恢复原订单当年实签过的页。

    原订单未签下的页（越界、额度不足）不在可恢复名册内；额度不够时低页先恢复，
    其余页段留作等待，不挤占同卷后单已经签下的页。
    """
    pool = target['pool']
    account = balances[pool]
    start, end = target['journal_pages']
    recovered, _outside, exhausted = split_limit(target['granted'], start, end,
                                                 account['limit'] - account['used'])
    awaiting = exhausted
    credit, waiting = pages_in(recovered), pages_in(awaiting)
    account['used'] += credit
    target['recovered_pages'] = recovered
    state[row['target']] = RESTORED
    # 恢复后订单回到在册有效名册，后续撤销/更正按恢复后的状态裁决
    active[row['target']] = target
    archive.pop(row['target'], None)
    return dict(entry=row['entry'], line=row['line'], target=row['target'], restored=credit,
                awaiting=waiting, recovered=recovered, awaiting_spans=awaiting, reason=None,
                target_line=target['line'], pool=pool)


def allocate(verdicts, pools):
    accounts, active, archive, state = {}, {}, {}, {}
    orders, reversals, corrections = [], [], []
    for row in verdicts:
        if row['action'] == 'cancel':
            reversals.append(cancellation(row, active, archive, state, accounts))
            continue
        if row['action'] == 'restore':
            code = row['ruling']
            phase = state.get(row['target'])
            if not code and phase == RESTORED:
                code = 'REPEATED_RESTORE'
            elif not code and phase != CANCELLED:
                code = 'NOT_CANCELLED' if phase == ACTIVE else 'UNKNOWN_TARGET'
            target = archive.get(row['target'])
            if not code and target is None:
                code = 'UNKNOWN_TARGET'
            if not code and row['org']['code'] != target['org']:
                code = 'WRONG_ORG'
            if not code and row['received'][:4] != target['received'][:4]:
                code = 'BOUNDARY'
            if code:
                corrections.append(dict(entry=row['entry'], line=row['line'], target=row['target'],
                                        restored=0, awaiting=0, recovered=[], awaiting_spans=[],
                                        reason=code, target_line=target['line'] if target else None,
                                        pool=target['pool'] if target else None))
            else:
                corrections.append(restoration(row, target, active, archive, state, accounts))
            continue
        intervals = normalize(row['pages'])
        chosen = row['license']
        reasons = {}
        allowed = denied = pending = 0
        pool = None
        granted, outside, shortage = [], [], []
        total = pages_in(intervals)
        if chosen:
            quota_id = chosen['pool']
            pool = f"{row['received'][:4]}/{quota_id}"
            account = accounts.setdefault(pool, {'limit':pools[quota_id],'used':0,'gross':0})
            start,end = row['journal']['pages']
            granted,outside,shortage = split_limit(intervals,start,end,account['limit']-account['used'])
            allowed, denied = pages_in(granted), pages_in(outside)+pages_in(shortage)
            if outside:
                reasons['OUTSIDE_ARTICLE'] = pages_in(outside)
            if shortage:
                reasons['QUOTA'] = pages_in(shortage)
            account['used'] += allowed
            account['gross'] += allowed
        elif row['ruling'] == 'NO_LICENSE':
            denied = total
            reasons[row['ruling']] = total
        else:
            pending = total
            reasons[row['ruling']] = total
        result = {'entry':row['entry'],'line':row['line'],'received':row['received'],
                  'org':row['org']['code'] if row['org'] else None,'license':chosen['id'] if chosen else None,
                  'pool':pool,'requested':total,'licensed':allowed,'denied':denied,'pending':pending,
                  'normalized':intervals,'granted':granted,'outside':outside,'shortage':shortage,
                  'journal_pages':row['journal']['pages'] if chosen else None,
                  'reasons':reasons,'notes':{code:MESSAGES[code] for code in reasons}}
        orders.append(result)
        if allowed:
            active[row['entry']] = result
            state[row['entry']] = ACTIVE
    revived = sorted(target for target, phase in state.items() if phase == RESTORED)
    cancelled = sorted(target for target, phase in state.items() if phase == CANCELLED)
    # 仍在等额度的恢复目标：已受理更正但尚有原实签页没有重新签下（含全数等待与部分等待）
    waiting = sorted({item['target'] for item in corrections
                      if not item['reason'] and item['awaiting']})
    return {'copies':orders,'reversals':reversals,'corrections':corrections,
            'accounts':{key:dict(value,available=value['limit']-value['used']) for key,value in accounts.items()},
            'cancelled':cancelled,'revived':revived,'awaiting_targets':waiting}
