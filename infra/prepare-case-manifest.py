#!/usr/bin/env python3
"""Build private import/setup manifests. No SQL, HTTP, model calls or credentials."""
import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
os.umask(0o077)
ROOT=Path(__file__).resolve().parents[1]

def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp_name=tempfile.mkstemp(prefix=f'.{path.name}.',suffix='.tmp',dir=path.parent)
    temporary=Path(temp_name)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
            stream.flush();os.fsync(stream.fileno())
            os.fchmod(stream.fileno(),0o600)
        os.replace(temporary,path)
        os.chmod(path,0o600)
    except BaseException:
        try:temporary.unlink()
        except OSError:pass
        raise

parser=argparse.ArgumentParser()
parser.add_argument('--case-dir',type=Path,default=ROOT/'.local/data/case')
parser.add_argument('--bundle',action='store_true',help='Build Core setup and model approval after AI import')
args=parser.parse_args();case=args.case_dir.resolve()
metadata=json.loads((ROOT/'infra/case-bank-metadata.json').read_text())
if not args.bundle:
    mapping=json.loads((case/'core-company-map.json').read_text())
    write(case/'manifest.json',{'schema_version':'private-case-input-v1','tenant_id':'core-local','case_id':'organizer-2026-v1',
        'company_csv':{'path':'df_Uid_data_hack.csv','origin':'Organizer case: df_Uid_data_hack.csv',
        'selected_synthetic_u_ids':list(mapping),'core_company_ids':mapping,'revenue_unit':None},
        'related_csvs':['df_Fid_Uid_link_hack.csv','df_Uid_payment_hack.csv'],
        'bank_documents':[{k:v for k,v in row.items() if k!='date_source'} for row in metadata]})
    print('manifest.json')
else:
    def payload_hash(value):
        return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    bundle=json.loads((case/'case-bundle.json').read_text())
    if bundle['summary']['bank_documents_failed']:raise SystemExit('Resolve failed imports before Core setup')
    sources=[]
    for profile in bundle['proposals']:
        company=profile['company_id']
        for item in metadata:
            source_id=hashlib.sha256((company+item['drive_id']).encode()).hexdigest()[:32]
            sources.append({'company_id':company,'source_id':source_id,'uri':item['canonical_uri'],
                'content_type':'application/pdf','source_role':'bank','classification':'confidential',
                'title':item['title'],'publisher':item['publisher'],
                'published_at':item['published_at']+'T00:00:00+00:00' if item['published_at'] else None})
        sources.append({'company_id':company,'source_id':hashlib.sha256((company+'cbr-press').encode()).hexdigest()[:32],
            'uri':'https://www.cbr.ru/rss/RssPress','content_type':'application/rss+xml','source_role':'external',
            'classification':'public','title':'Пресс-релизы Банка России','publisher':'Банк России',
            'article_hosts':['www.cbr.ru','cbr.ru']})
    write(case/'core-setup.json',{'schema_version':'judging-core-setup-v1','profiles':bundle['proposals'],'sources':sources})
    approval={'schema_version':'organizer-model-approval-v1','tenant_id':'core-local',
        'company_profiles':{p['company_id']:payload_hash(p['context']) for p in bundle['proposals']},
        'company_csv_sha256':bundle['inputs']['company_csv']['sha256'],
        'documents':[{k:d[k] for k in ['source_id','document_revision_ref','raw_sha256','canonical_uri']} for d in bundle['bank_documents'] if d['status']=='ready']}
    write(case/'model-approval.json',approval)
    print('core-setup.json; model-approval.json')
