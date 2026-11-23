from concurrent.futures import ThreadPoolExecutor
from app.source import bundle
from app.vault.record import deposit, index
from app.workflow import steps

HEADER = 'entry,action,received,institution,issn,pages,target\n'


def correction(report, entry):
    return next(item for item in report['pages']['corrections'] if item['entry'] == entry)


def test_restore_reinstates_signed_pages_when_quota_available():
    source = bundle('clean').decode()
    amended = source + 'R1,cancel,2026-12-03,00007,,,C04\nX2,restore,2026-12-04,00007,,,C04\n'
    report = steps(amended.encode(), 'balance')
    item = correction(report, 'X2')
    assert (item['restored'], item['awaiting']) == (3, 0)
    assert item['recovered'] == [[8, 11]] and item['reason'] is None
    assert report['pages']['revived'] == ['C04'] and report['pages']['cancelled'] == []
    b = report['balance']
    assert (b['restored'], b['recovered'], b['effective'], b['balanced']) == (3, 3, 12, True)


def test_restore_waits_when_later_orders_hold_the_freed_quota():
    # dirty 卷：C01 实签 6 页后被 R01 撤销，NORTH 池（限额10）随后被 C02/C03 占满
    amended = bundle('dirty').decode() + 'X1,restore,2026-11-12,00123,,,C01\n'
    report = steps(amended.encode(), 'balance')
    item = correction(report, 'X1')
    assert (item['restored'], item['awaiting']) == (0, 6)
    assert item['recovered'] == [] and item['awaiting_spans'] == [[3, 9]]
    assert report['pages']['awaiting_targets'] == ['C01']
    assert report['pages']['accounts']['2026/NORTH']['used'] == 10
    b = report['balance']
    assert (b['recovered'], b['awaiting_quota'], b['effective'], b['balanced']) == (0, 6, 26, True)


def test_partial_restore_takes_lowest_pages_and_keeps_rest_waiting():
    rows = [HEADER.strip(),
            'C04,copy,2026-12-01,00007,2049-3630,8-10,',
            'R1,cancel,2026-12-03,00007,,,C04',
            'C30,copy,2026-12-04,00007,2049-3630,3-8,',
            'X2,restore,2026-12-06,00007,,,C04']
    report = steps(('\n'.join(rows) + '\n').encode(), 'balance')
    item = correction(report, 'X2')
    # NET 池限额 8，后单占 6 页后只剩 2 页：低页 8-9 先恢复，页 10 继续等额度
    assert (item['restored'], item['awaiting']) == (2, 1)
    assert item['recovered'] == [[8, 10]] and item['awaiting_spans'] == [[10, 11]]
    assert report['pages']['awaiting_targets'] == ['C04']
    assert report['balance']['balanced']


def test_restore_cannot_backfill_pages_never_signed():
    rows = [HEADER.strip(),
            'C40,copy,2026-11-06,00123,2049-3630,1-3,',
            'R1,cancel,2026-11-07,00123,,,C40',
            'X1,restore,2026-11-08,00123,,,C40']
    report = steps(('\n'.join(rows) + '\n').encode(), 'balance')
    c40 = next(c for c in report['pages']['copies'] if c['entry'] == 'C40')
    assert c40['outside'] == [[1, 3]]  # 页 1-2 原本越界未签
    item = correction(report, 'X1')
    assert item['recovered'] == [[3, 4]] and item['restored'] == 1


def test_repeated_restore_charges_pages_once():
    source = bundle('clean').decode()
    once = source + 'R1,cancel,2026-12-03,00007,,,C04\nX2,restore,2026-12-04,00007,,,C04\n'
    twice = once + 'X3,restore,2026-12-05,00007,,,C04\n'
    report = steps(twice.encode(), 'balance')
    codes = [(c['entry'], c['reason'], c['restored']) for c in report['pages']['corrections']]
    assert codes == [('X2', None, 3), ('X3', 'REPEATED_RESTORE', 0)]
    single = steps(once.encode(), 'balance')
    assert report['pages']['accounts'] == single['pages']['accounts']


def test_restore_guards_target_state():
    source = bundle('clean').decode()
    bad = source + 'X4,restore,2026-12-04,00007,,,C05\nX5,restore,2026-12-04,00007,,,absent\n'
    report = steps(bad.encode(), 'balance')
    assert [(c['entry'], c['reason']) for c in report['pages']['corrections']] == [
        ('X4', 'NOT_CANCELLED'), ('X5', 'UNKNOWN_TARGET')]
    re_cancel = source + ('R2,cancel,2026-12-03,00007,,,C04\n'
                          'X6,restore,2026-12-04,00007,,,C04\n'
                          'R3,cancel,2026-12-05,00007,,,C04\n')
    report = steps(re_cancel.encode(), 'balance')
    assert [v['reason'] for v in report['pages']['reversals']] == [None, 'RESTORED_TARGET']


def test_restore_org_and_year_must_match_target():
    rows = [HEADER.strip(),
            'C04,copy,2026-12-01,00007,2049-3630,8-10,',
            'R1,cancel,2026-12-03,00007,,,C04',
            'X7,restore,2026-12-04,00123,,,C04',
            'X8,restore,2027-01-02,00007,,,C04']
    report = steps(('\n'.join(rows) + '\n').encode(), 'balance')
    assert [c['reason'] for c in report['pages']['corrections']] == ['WRONG_ORG', 'BOUNDARY']


def test_restore_intake_shape_is_validated():
    for bad, code in [
        ('X1,restore,2026-11-08,00123,2049-3630,3-4,\n', 'MISSING'),
        ('X3,restore,2026-11-08,00123,,, \n', 'MISSING'),
    ]:
        report = steps((HEADER + bad).encode(), 'intake')
        assert [e['code'] for e in report['intake']['exceptions']] == [code]
    ok = steps((HEADER + 'X4,restore,2026-11-08,00123,,,C01\n').encode(), 'intake')
    assert ok['intake']['accepted'] == 1


def test_concurrent_correction_deposits_commit_once(desk):
    amended = (bundle('clean').decode()
               + 'R1,cancel,2026-12-03,00007,,,C04\nX2,restore,2026-12-04,00007,,,C04\n').encode()
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: deposit(desk.vault, amended), range(16)))
    assert sum(not item['reused'] for item in results) == 1
    assert len(index(desk.vault)) == 1


def test_old_certificate_keeps_verifying_after_correction(client):
    first = client.post('/api/volumes', json={'sample': 'clean'}).json
    old = '/api/volumes/' + first['run_id']
    signature = client.post(old + '/stamp').json['imprint']
    amended = bundle('clean').decode() + 'R1,cancel,2026-12-03,00007,,,C04\nX2,restore,2026-12-04,00007,,,C04\n'
    newer = client.post('/api/volumes', json={'csv': amended}).json
    assert client.get(old + '/export').json['imprint'] == signature
    assert client.get(old + '/export').json['verified']
    new_path = '/api/volumes/' + newer['run_id']
    stamp = client.post(new_path + '/stamp').json
    assert client.post(new_path + '/stamp').json == dict(stamp, reused=True)
    document = client.get(new_path + '/export').json['document']
    assert document['restorations'][0]['restored'] == 3
