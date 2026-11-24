"""Regression tests for the verified LNC / MECDM / TNTV / Samoa omissions."""
import io
import json
import logging
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from crawler import cursor, store, web
from crawler.request import RequestQueue
from crawler.channels import base, json_list, wp_api
from crawler.documents import extract_pdf

LOG = logging.getLogger(__name__)


def test_failure_body_containing_404_is_not_a_gone_page():
    assert not base._gone_404(['http_500 upstream error at /404.html'])
    assert base._gone_404(['http_404 gone', 'http_410 gone'])


def pdf_bytes():
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    content = DecodedStreamObject()
    content.set_data(b'BT /F1 12 Tf 20 200 Td (Recoverable PDF content) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(content)
    writer.add_metadata({'/Title': 'Recovered report'})
    out = io.BytesIO(); writer.write(out)
    return out.getvalue()


def test_lnc_400_requeue_is_scoped_and_cooldown_respected(tmp_env):
    q = RequestQueue('lnc.test')
    q.add_many(['https://lnc.test/old', 'https://lnc.test/gone'])
    a, b = q.next_batch(2)
    for _ in range(3):
        q.mark_error([a.key,b.key], {a.key:'http_400 bad session', b.key:'http_404 gone'})
    assert q.retry_transient(cooldown=0) == 0
    assert q.retry_transient(cooldown=3600, statuses=[400]) == 0
    assert q.retry_transient(cooldown=0, statuses=[400]) == 1
    assert q.next_batch(2)[0].url == a.url
    assert q.stats() == {'unprocessed': 1, 'error': 1}


def test_pdf_old_skipped_recovers_and_original_deduplicates(tmp_env):
    site = {'domain':'pdf.test', 'channel':'sitemap', 'documents':True}
    urls = ['https://pdf.test/report/download.html','https://pdf.test/?task=document.viewdoc&id=1']
    q = RequestQueue(site['domain']); q.add_many(urls)
    q.mark_skipped([r.key for r in q.next_batch(2)], 'no_text')
    body = pdf_bytes()
    class Fetcher:
        def stream_requests(self, batch, workers=4):
            yield from ((req,200,body) for req in batch)
    result = base.collect_urls(site, urls, 0, LOG, Fetcher())
    assert result['saved'] == 2 and q.stats() == {'done':2}
    rows = [json.loads(x) for x in (tmp_env/'pdf.test/articles.jsonl').read_text().splitlines()]
    assert all('Recoverable PDF content' in r['text'] for r in rows)
    assert len(list((tmp_env/'pdf.test/attachments').glob('*.pdf'))) == 1
    assert len(list((tmp_env/'pdf.test/attachments').glob('*.json'))) == 2
    assert base.collect_urls(site, urls, 0, LOG, Fetcher())['saved'] == 0


def test_bad_pdf_is_retryable_failure_not_no_text_or_done(tmp_env):
    site = {'domain':'badpdf.test','channel':'sitemap','documents':True}
    class Fetcher:
        def stream_requests(self, batch, workers=4):
            yield from ((req,200,b'%PDF-1.4\nbroken') for req in batch)
    result = base.collect_urls(site,['https://badpdf.test/file.pdf'],0,LOG,Fetcher())
    assert result['status'] == 'partial' and result['queue'] == {'error':1}
    q = RequestQueue(site['domain'])
    assert q.retry_transient(cooldown=0) == 1


def test_pdf_transport_preserves_binary_and_rejects_truncation(monkeypatch):
    from test_fetch import FakeResponse
    body = pdf_bytes()
    monkeypatch.setattr(web, '_rotate_session', lambda: None)
    monkeypatch.setattr(web.SESSION, 'get', lambda *a,**k: FakeResponse(ctype='application/pdf', body=body))
    assert web.get_text('https://pdf.test/a',{},documents=True) == (200,body)
    monkeypatch.setattr(web, 'MAX_BODY_BYTES', len(body)-1)
    with pytest.raises(ValueError):web.get_text('https://pdf.test/a',{},documents=True)
    monkeypatch.setattr(web.cffi_requests, 'get', lambda *a,**k:type('R',(),{'status_code':200,'headers':{'content-type':'application/pdf'},'content':body})())
    monkeypatch.setattr(web, 'MAX_BODY_BYTES', len(body)+1)
    f = web.Fetcher({'domain':'pdf.test','via':'impersonate','documents':True})
    assert f._impersonate_text('https://pdf.test/a',{}) == (200,body)


def test_wp_backfill_replays_once_and_resumes_without_duplicates(tmp_env, monkeypatch):
    site = {'domain':'wp.test','channel':'wp-api','base':'https://wp.test','backfill_revision':{'posts':1}}
    store.append_rows(site['domain'],[{'url':'https://wp.test/1','title':'existing'}])
    cursor.save(site['domain'],status='complete',next_page=3,total_pages=2)
    calls=[]
    def get(site,url,**kwargs):
        from urllib.parse import parse_qs,urlsplit
        page=int(parse_qs(urlsplit(url).query)['page'][0]);calls.append(page)
        items=[{'link':f'https://wp.test/{page}','title':{'rendered':f'title {page}'},'content':{'rendered':''}}]
        return 200,json.dumps(items),{'x-wp-totalpages':'2','x-wp-total':'2'}
    monkeypatch.setattr(wp_api,'_get_for_site',get)
    result=wp_api.collect_wp(site,workers=1,progress=False)
    assert result['saved']==1 and result['status']=='complete' and calls==[1,2]
    assert wp_api.collect_wp(site)['skipped']=='done'
    assert len(store.load_known(site['domain']))==2
    # A bumped revision can pause and resume without resetting page 2 back to 1.
    site['backfill_revision']['posts']=2
    cursor.save(site['domain'],status='interrupted',backfill_revision=2,next_page=2,total_pages=2)
    calls.clear();wp_api.collect_wp(site,workers=1,progress=False)
    assert calls==[2] and len(store.load_known(site['domain']))==2


def test_samoa_failed_details_survive_eof_and_rerun(tmp_env, monkeypatch):
    monkeypatch.setattr(json_list,'DATA',str(tmp_env));monkeypatch.setenv('RETRY_COOLDOWN','0')
    site={'domain':'json.test','channel':'json-list','base':'https://json.test','api_list':'/p/{page}',
          'api_categories':['news'],'api_url':'{base}/item/{id}','skip_detail_failures':True,'pages':10}
    monkeypatch.setattr(json_list,'fetch_page',lambda p,b,n,c,k:({'news':[{'id':1,'heading':'title'}] if n==1 else []},[]))
    class Fetcher:
        ok=False
        def __init__(self,*a):pass
        def stream_many(self,urls,workers=4):
            yield from ((u,200,'<html><title>Recovered</title><p>Article text.</p></html>') if self.ok else (u,0,'timeout') for u in urls)
    monkeypatch.setattr(json_list,'Fetcher',Fetcher)
    result=json_list.collect_json_list(site)
    assert result['status']=='partial' and result['queue']=={'error':1}
    assert json.loads((tmp_env/'json.test/jsonlist.cursor.json').read_text())['page']==2
    Fetcher.ok=True
    result=json_list.collect_json_list(site)
    assert result['status']=='complete' and result['saved']==1 and result['queue']=={'done':1}
    assert json_list.collect_json_list(site)['saved']==0


def test_samoa_disk_failure_does_not_mark_done_or_advance(tmp_env,monkeypatch):
    monkeypatch.setattr(json_list,'DATA',str(tmp_env))
    site={'domain':'disk.test','channel':'json-list','base':'https://disk.test','api_list':'/p/{page}',
          'api_categories':['news'],'api_url':'{base}/item/{id}','pages':2}
    monkeypatch.setattr(json_list,'fetch_page',lambda *a:({'news':[{'id':1}]},[]))
    class Fetcher:
        def __init__(self,*a):pass
        def stream_many(self,urls,workers=4):yield from ((u,200,'<title>Title</title><p>Body.</p>') for u in urls)
    monkeypatch.setattr(json_list,'Fetcher',Fetcher)
    monkeypatch.setattr(json_list,'append_rows',lambda *a:(_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):json_list.collect_json_list(site)
    assert RequestQueue(site['domain']).stats()=={'unprocessed':1}
    assert not (tmp_env/'disk.test/jsonlist.cursor.json').exists()


def test_json_list_tracks_listing_flag_and_actual_detail_access(tmp_env, monkeypatch):
    monkeypatch.setattr(json_list, 'DATA', str(tmp_env))
    site = {'domain': 'reading.test', 'base': 'https://reading.test', 'api_list': '/p/{page}',
            'api_url': '{base}/article/{id}', 'api_categories': ['news'],
            'free_field': 'is_premium', 'include_premium': True,
            'premium_marker': 'id="impression-panel"'}
    items = [{'id': 1, 'is_premium': 'false'}, {'id': 2, 'is_premium': True},
             {'id': 3, 'is_premium': True}, {'id': 4}]
    monkeypatch.setattr(json_list, 'fetch_page',
                        lambda p, b, n, c, k: ({'news': items if n == 1 else []}, []))
    class Fetcher:
        def __init__(self, site):
            pass
        def stream_many(self, urls, workers=4):
            for u in urls:
                if u.endswith('/3'):
                    yield u, 200, "<html><article><div id='impression-panel'>Subscribe</div></article></html>"
                else:
                    yield u, 200, '<html><title>Public report</title><p>A visible article body.</p></html>'
    monkeypatch.setattr(json_list, 'Fetcher', Fetcher)
    result = json_list.collect_json_list(site)
    assert result['saved'] == 3 and result['status'] == 'partial'
    assert result['access_restricted'] == 1
    queue = RequestQueue(site['domain'])
    labels = [json.loads(row[0]) for row in queue.conn.execute(
        'SELECT label FROM requests WHERE domain=? ORDER BY url', (site['domain'],))]
    assert [label['listing_premium'] for label in labels] == [False, True, True, None]
    assert labels[2]['detail_access'] == 'paywall_element'
    assert all(label['detail_access'] in ('body_readable', 'metadata_only')
               for i, label in enumerate(labels) if i != 2)
    assert store.load_known(site['domain']) == {site['base'] + '/article/' + str(n) for n in (1, 2, 4)}


def test_broken_json_list_checkpoint_does_not_restart_from_page_one(tmp_env, monkeypatch):
    monkeypatch.setattr(json_list, 'DATA', str(tmp_env))
    site = {'domain': 'broken.test', 'base': 'https://broken.test'}
    checkpoint = tmp_env / site['domain'] / 'jsonlist.cursor.json'
    checkpoint.parent.mkdir()
    checkpoint.write_text('{')
    monkeypatch.setattr(json_list, 'fetch_page', lambda *a: pytest.fail('list request on invalid cursor'))
    result = json_list.collect_json_list(site)
    assert result['status'] == 'interrupted' and 'list_cursor_unreadable' in result['error']
    assert checkpoint.read_text() == '{'


def test_supervisor_does_not_skip_configured_backfill(tmp_env,monkeypatch):
    import supervisor
    sources=tmp_env/'sources.yaml';sources.write_text('- domain: wp.test\n  backfill_revision: {posts: 1}\n')
    monkeypatch.setattr(supervisor,'SOURCES',sources)
    cursor.save('wp.test',status='complete')
    assert not supervisor.is_done(tmp_env,'wp.test')
    cursor.save('wp.test',status='complete',backfill_revision=1)
    assert supervisor.is_done(tmp_env,'wp.test')
