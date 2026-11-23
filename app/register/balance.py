from collections import Counter


def reconcile(intake, ledger):
    copies = ledger['copies']
    total = {key:sum(row[key] for row in copies) for key in ('requested','licensed','denied','pending')}
    restored = sum(row['restored'] for row in ledger['reversals'])
    recovered = sum(row['restored'] for row in ledger['corrections'])
    awaiting = sum(row['awaiting'] for row in ledger['corrections'])
    accounts = ledger['accounts']
    source_ok = intake['received'] == intake['accepted']+len(intake['duplicates'])+intake['isolated']
    orders_ok = all(r['requested']==r['licensed']+r['denied']+r['pending'] for r in copies)
    reasons = Counter()
    footprints = []
    for row in copies:
        reasons.update(row['reasons'])
        if row['reasons']:
            footprints.append({'entry':row['entry'],'line':row['line'],'reasons':row['reasons']})
    # 有效占用以额度池实际占用为准：撤销返还减页，恢复按实际重新签下的页加回；
    # 仍在等额度的恢复目标不产生占用，也不改变后单已签下的页。
    effective = {pool:a['used'] for pool,a in accounts.items()}
    identity = total['licensed']-restored+recovered
    quota_ok = all(a['limit']==a['used']+a['available'] for a in accounts.values()) and sum(effective.values())==identity
    return dict(total, restored=restored, recovered=recovered, awaiting_quota=awaiting,
                effective=identity,
                balanced=source_ok and orders_ok and quota_ok, source_ok=source_ok, quota_ok=quota_ok,
                accounts=accounts, reasons=dict(reasons), explanations=footprints,
                cancellation_errors=[row for row in ledger['reversals'] if row['reason']],
                correction_errors=[row for row in ledger['corrections'] if row['reason']])
