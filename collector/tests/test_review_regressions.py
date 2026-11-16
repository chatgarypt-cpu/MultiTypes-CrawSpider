"""Regression tests for public collection, coverage and storage."""
import json, logging, sys
from pathlib import Path
from unittest.mock import patch
import pytest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'collector'))
from crawler import config,store,request,cursor,discover
from crawler.channels import base,sitemap,html_list,json_list
import verify_coverage as vc

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
 for mod in (config,store,request,json_list,vc):
  monkeypatch.setattr(mod,'DATA',str(tmp_path),raising=False)
  monkeypatch.setattr(mod,'DB',str(tmp_path/'state.db'),raising=False)
 store._KNOWN.clear()
 return tmp_path

class Fetch:
 def __init__(self,pages): self.pages=pages
 def get_many(self,urls): return [(u,*self.pages[u]) for u in urls]
 def stream_many(self,urls,workers=4): return iter(self.get_many(urls))
 def stream_requests(self,batch,workers=4): return iter((r,*self.pages[r.url]) for r in batch)

BODY='<html><title>News report</title><article>'+('<p>A detailed news report about local education and the community.</p>'*12)+'</article></html>'
LOG=logging.getLogger('review')

def test_sitemap_403_must_not_be_complete():
 root='https://x.test'
 pages={root+'/sitemap.xml':(200,'<sitemapindex><loc>'+root+'/ok.xml</loc><loc>'+root+'/blocked.xml</loc></sitemapindex>'),root+'/ok.xml':(200,'<urlset><loc>'+root+'/news/a</loc></urlset>'),root+'/blocked.xml':(403,'Access denied'),root+'/news/a':(200,BODY)}
 out=sitemap.run({'domain':'x.test','base':root,'channel':'sitemap'},0,LOG,Fetch(pages))
 assert out['status']!='complete',out

def test_html_listing_failure_must_not_be_complete():
 root='https://x.test'
 pages={root+'/list1':(200,'<a href="/news/a">Article</a>'),root+'/list2':(503,'Unavailable'),root+'/news/a':(200,BODY)}
 site={'domain':'x.test','base':root,'channel':'html','list_urls':['/list1','/list2'],'link_pattern':'/news/','noise_gate':False}
 out=html_list.run(site,0,LOG,Fetch(pages))
 assert out['status']!='complete',out

def test_sitemap_must_not_truncate_child_81():
 root='https://x.test'
 children=[root+f'/s{i}.xml' for i in range(81)]
 pages={root+'/sitemap.xml':(200,'<sitemapindex>'+''.join('<loc>'+u+'</loc>' for u in children)+'</sitemapindex>')}
 pages.update({u:(200,'<urlset><loc>'+root+f'/news/{i}</loc></urlset>') for i,u in enumerate(children)})
 urls=list(discover.iter_sitemap_urls({'domain':'x.test','base':root},Fetch(pages)))
 assert len(urls)==81,len(urls)

def test_coverage_requires_matching_urls(isolated,monkeypatch):
 d=isolated/'x.test';d.mkdir()
 (d/'urls.txt').write_text(''.join(f'https://x.test/old/{i}\n' for i in range(10)))
 upstream=[f'https://x.test/new/{i}' for i in range(10)]
 monkeypatch.setattr(vc,'sitemap_urls',lambda site,state=None:upstream)
 out=vc.verify_site({'domain':'x.test','base':'https://x.test','channel':'sitemap'},{'posts':10,'pages':0,'media':0})
 assert out['verdict']!='已核对',out

def test_json_detail_failure_keeps_cursor(isolated,monkeypatch):
 site={'domain':'x.test','base':'https://x.test','channel':'json-list','api_list':'/list/{page}','api_categories':['a'],'api_url':'{base}/news/{id}','max_pages_per_run':1}
 monkeypatch.setattr(json_list,'fetch_page',lambda *a,**k:({'a':[{'id':1}]},[]))
 monkeypatch.setattr(json_list,'Fetcher',lambda site:Fetch({'https://x.test/news/1':(503,'Unavailable')}))
 out=json_list.collect_json_list(site)
 p=isolated/'x.test/jsonlist.cursor.json'
 cur=json.loads(p.read_text()) if p.exists() else {'page':1}
 assert cur['page']==1,{'result':out,'cursor':cur}

def test_challenge_page_must_not_be_saved(isolated):
 site={'domain':'x.test','base':'https://x.test','channel':'html'}
 url='https://x.test/news/a';body='<html><title>Just a moment...</title><body>Please enable JavaScript and cookies to continue</body></html>'
 out=base.collect_urls(site,[url],1,LOG,Fetch({url:(200,body)}))
 assert out['saved']==0,out



def test_completed_listing_picks_up_new_urls():
 site={'domain':'x.test','base':'https://x.test','channel':'html','noise_gate':False}
 a,b='https://x.test/news/a','https://x.test/news/b'
 f=Fetch({a:(200,BODY),b:(200,BODY)})
 assert base.collect_urls(site,[a],0,LOG,f)['status']=='complete'
 assert base.collect_urls(site,[a,b],0,LOG,f)['saved']==1
 assert len(store.load_known('x.test'))==2


def test_malformed_sitemap_is_failure():
 root='https://x.test'; failures=[]
 f=Fetch({root+'/sitemap.xml':(200,'<html><title>Security Check</title></html>')})
 assert list(discover.iter_sitemap_urls({'base':root},f,failures=failures))==[]
 assert failures and failures[0]['error']=='invalid_sitemap'


def test_sitemap_entities_and_namespace():
 root='https://x.test'
 f=Fetch({root+'/sitemap.xml':(200,'<s:urlset xmlns:s="http://www.sitemaps.org/schemas/sitemap/0.9"><s:url><s:loc>https://x.test/news/a?x=1&amp;y=2</s:loc></s:url></s:urlset>')})
 assert list(discover.iter_sitemap_urls({'base':root},f))==[root+'/news/a?x=1&y=2']


def test_coverage_partial_sitemap_not_verified(monkeypatch):
 def upstream(site,state=None):
  state['failed']=[{'url':'https://x.test/missing.xml','status':403}]
  return ['https://x.test/news/a']
 monkeypatch.setattr(vc,'sitemap_urls',upstream)
 rec=vc.verify_site({'domain':'x.test','channel':'sitemap'},{'posts':1})
 assert rec['coverage'] is None and rec['verdict']=='不可验证'


def test_json_detail_recovers_without_duplicate(isolated,monkeypatch):
 site={'domain':'x.test','base':'https://x.test','channel':'json-list','api_list':'/list/{page}','api_categories':['a'],'api_url':'{base}/news/{id}','max_pages_per_run':1,'noise_gate':False}
 monkeypatch.setattr(json_list,'fetch_page',lambda *a,**k:({'a':[{'id':1},{'id':2}]},[]))
 pages={'https://x.test/news/1':(200,BODY),'https://x.test/news/2':(503,'Unavailable')}
 monkeypatch.setattr(json_list,'Fetcher',lambda site:Fetch(pages))
 assert json_list.collect_json_list(site)['saved']==1
 pages['https://x.test/news/2']=(200,BODY)
 assert json_list.collect_json_list(site)['saved']==1
 assert len(store.load_known('x.test'))==2
 assert json.loads((isolated/'x.test/jsonlist.cursor.json').read_text())['page']==2


def test_sequential_impersonation_uses_selected_transport(monkeypatch):
 from crawler import web
 f=web.Fetcher({'domain':'x.test','via':'impersonate','robots':False})
 monkeypatch.setattr(web.LIMITER,'acquire',lambda *a,**k:None)
 monkeypatch.setattr(web.LIMITER,'reward',lambda *a,**k:None)
 monkeypatch.setattr(f,'_impersonate_text',lambda u,headers:(200,'fingerprint'))
 monkeypatch.setattr(web,'get_text',lambda *a,**k:pytest.fail('wrong transport'))
 assert list(f.stream_many(['https://x.test'],workers=1))[0][2]=='fingerprint'




def test_short_real_notice_survives_metadata():
 from crawler.extract import extract_metadata
 site={'domain':'x.test','channel':'html','base':'https://x.test'}
 row=extract_metadata('https://x.test/notice','<html><title>Meeting notice</title><article>Meeting at 10 am tomorrow.</article></html>',site)
 assert row and row['title']=='Meeting notice'


def test_security_check_not_metadata():
 from crawler.extract import extract_metadata
 row=extract_metadata('https://x.test/notice','<html><title>Security Check</title><iframe src="/challenge"></iframe></html>',{'domain':'x.test'})
 assert row is None




def test_json_api_failure_resumes_same_page(monkeypatch):
 from crawler.channels import json_api
 site={'domain':'x.test','base':'https://x.test','channel':'json-api','api':'/api','api_query':'limit=1&page=9&filter[status]=published'}
 def item(i):return {'id':i,'title':f'Article {i}','content':{'inner':'Content'},'status':'published'}
 pages={json_api.page_url(site,1):(200,json.dumps({'data':[item(1)]})),json_api.page_url(site,2):(503,'down'),json_api.page_url(site,3):(200,'{"data":[]}')}
 first=json_api.collect_json(site,fetcher=Fetch(pages))
 assert first['status']=='interrupted' and cursor.load('x.test')['next_page']==2
 pages[json_api.page_url(site,2)]=(200,json.dumps({'data':[item(2)]}))
 second=json_api.collect_json(site,fetcher=Fetch(pages))
 assert second['status']=='complete' and second['saved']==1
 assert len(store.load_known('x.test'))==2
 assert 'page=1' in json_api.page_url(site,1) and 'filter%5Bstatus%5D=published' in json_api.page_url(site,1)


def test_custom_wp_route_schema_rejects_path_escape():
 from crawler.schema import validate_site
 errors,_=validate_site({'domain':'x.test','channel':'wp-api','base':'https://x.test','wp_types':{'../bad':'../bad'}})
 assert len(errors)==2


def test_paragraph_fallback_excludes_navigation():
 from crawler.extract import _para_text
 article='A real community report with enough detail to explain the local project.'
 body='<html><nav><p>'+('Menu item '*100)+'</p></nav><article><p>'+article+'</p></article><footer><p>'+('Footer '*100)+'</p></footer></html>'
 assert _para_text(body)==article


def test_wp_type_inventory_exposes_unconfigured_namespace():
 site={'domain':'x.test','base':'https://x.test'}
 f=Fetch({'https://x.test/wp-json/wp/v2/types':(200,json.dumps({'report':{'rest_base':'reports','rest_namespace':'public/v1','has_archive':True}}))})
 item=discover.wp_type_inventory(site,f)['types'][0]
 assert item['route']=='public/v1/reports' and item['needs_scope_review']


def test_wp_missing_one_kind_cannot_verify(monkeypatch):
 monkeypatch.setattr(vc,'wp_totals',lambda s:{'posts':10,'pages':0,'media':None})
 rec=vc.verify_site({'domain':'x.test','channel':'wp-api'},{'posts':10})
 assert rec['coverage'] is None and rec['verdict']=='口径异常'


def test_article_excludes_subscription_form():
 from crawler.extract import extract_best
 text='The community launched a new education project with schools across the island. '
 body='<html><title>Community project</title><article><p>'+text*12+'</p></article><form><p>'+'Credit card billed automatically. '*100+'</p><select><option>Zimbabwe</option></select></form></html>'
 row=extract_best('https://x.test/news/a',body,{'domain':'x.test','channel':'sitemap'},min_text=0)
 assert row and 'Credit card billed' not in row['text'] and 'Zimbabwe' not in row['text']


def test_wp_high_count_with_failed_page_not_complete(monkeypatch):
 monkeypatch.setattr(vc,'wp_totals',lambda s:{'posts':100,'pages':0,'media':0})
 monkeypatch.setattr(vc,'wp_missing_sample',lambda *a,**k:{'sample':[]})
 cursor.save('x.test',status='partial',skipped_pages=1)
 rec=vc.verify_site({'domain':'x.test','channel':'wp-api'},{'posts':99})
 assert rec['verdict']=='有缺口' and rec['crawl_unresolved']['posts']['skipped_pages']==1


def test_legacy_sitemap_html_entities_keep_strict_xml():
 kind, urls = discover.sitemap_locs('<urlset><url><loc>https://x.test/a</loc><description>A &bull; B</description></url></urlset>')
 assert kind == 'urlset' and urls == ['https://x.test/a']
 assert discover.sitemap_locs('<urlset><url><loc>broken')[0] is None


def test_legacy_done_without_saved_row_is_requeued(isolated):
 q=request.RequestQueue('x.test')
 q.add_many(['https://x.test/kept/', 'https://x.test/lost'])
 q.mark_done([request.canonical_key('https://x.test/kept/'),request.canonical_key('https://x.test/lost')])
 assert q.requeue_missing_rows({'https://x.test/kept'}) == 1
 assert [r.url for r in q.next_batch()] == ['https://x.test/lost']
 assert q.stats()['done'] == 1


def test_completed_sitemap_repairs_missing_disk_row_before_fast_return(isolated):
 domain='x.test';url='https://x.test/lost'
 q=request.RequestQueue(domain);q.add_many([url]);q.mark_done([request.canonical_key(url)])
 cursor.mark_complete(domain,'posts',saved=1,total_pages=1,next_page=2)
 out=base.collect_urls({'domain':domain,'base':'https://x.test','channel':'sitemap'},[],0,LOG,Fetch({url:(200,BODY)}),discovered_count=1)
 assert out['saved']==1 and out['status']=='complete'
 assert (isolated/domain/'articles.jsonl').exists()
