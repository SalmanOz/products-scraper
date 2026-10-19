"""Repair ONLY reviewed selected class letters. No guesses, score/price changes.

The audit is authenticated GET-only. Public source observations are collected
separately using ordinary access; the apply API preserves old sibling evidence.
"""
import argparse
import json
from pathlib import Path
import re

from source_ingestion import SourceIngestionClient


def run(client, *, apply=False, batch=None, report_path=Path('classification-repair-report.json')):
    endpoint=client.base_url + '/api/ingestion/classification-repair'
    manifest=client._json_body(client._request_with_retry('GET',endpoint,authenticated=True))
    if manifest.get('contract')!='classification-repair-v1' or not isinstance(manifest.get('products'),list):
        raise ValueError('classification_repair_contract_unavailable')
    report={'mode':'apply' if apply else 'audit','candidates':manifest['products'],'results':[],'changed_products':0,'changed_fields':0}
    def save():
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    if not apply:
        return report
    if not isinstance(batch,dict) or set(batch)!={'reviewed_on','products'} or not isinstance(batch['products'],list) or len(batch['products'])>100:
        raise ValueError('reviewed_classification_batch_required')
    candidates={p['slug']:p for p in manifest['products']}
    slugs=set()
    for reviewed in batch['products']:
        if not isinstance(reviewed,dict) or set(reviewed)!={'slug','name','readings'} or reviewed['slug'] in slugs:
            raise ValueError('invalid_or_duplicate_reviewed_product')
        slugs.add(reviewed['slug'])
        current=candidates.get(reviewed['slug'])
        result={'slug':reviewed['slug'],'status':'held'}
        report['results'].append(result)
        if not current or current['name']!=reviewed['name']:
            result['reason']='no_matching_pending_product';save();continue
        readings=reviewed['readings']
        if not isinstance(readings,list) or not 1<=len(readings)<=3:
            raise ValueError('invalid_readings')
        groups={r['group'] for r in readings}
        payload={'mode':'validate','slug':current['slug'],
                 'expected_groups':{g:current['expected_groups'][g] for g in groups},'readings':readings}
        try:
            validation=client._json_body(client._request_with_retry('POST',endpoint,json=payload,authenticated=True))
            if validation.get('eligible') is not True or validation.get('committed') is not False:
                raise ValueError('repair_validation_not_confirmed')
            payload['mode']='apply'
            response=client._json_body(client._request_with_retry('POST',endpoint,json=payload,authenticated=True))
            if response.get('committed') is not True:
                raise ValueError('repair_commit_not_confirmed')
            result.update(status='committed',changed_fields=response['changed_fields'],preserved_source_records=response['preserved_source_records'])
            report['changed_products']+=1;report['changed_fields']+=len(response['changed_fields']);save()
        except Exception as error:
            result['reason']=getattr(error,'response_body',{}).get('error',type(error).__name__)
            save()
    refreshed={p.slug:p for p in client.fetch_catalog(page_size=500)}
    for reviewed in batch['products']:
        result=next(r for r in report['results'] if r['slug']==reviewed['slug'])
        if result['status']!='committed':
            continue
        product=refreshed.get(reviewed['slug'])
        if not product or product.data_quality_status!='verified' or any(product.attributes.get(r['group'],{}).get(r['key'])!=r['value'] for r in reviewed['readings']):
            result['status']='committed_needs_attention';save();raise ValueError('post_repair_verification_failed')
        result['status']='verified';save()
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--batch',default='none')
    parser.add_argument('--report',default='classification-repair-report.json')
    args=parser.parse_args()
    batch=None
    if args.batch!='none':
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',args.batch):
            raise ValueError('invalid_batch_identifier')
        path=Path(__file__).parent/'classification_batches'/(args.batch+'.json')
        if path.stat().st_size>200_000:
            raise ValueError('batch_too_large')
        batch=json.loads(path.read_text(encoding='utf-8'))
    report=run(SourceIngestionClient.from_env(),apply=args.apply,batch=batch,report_path=Path(args.report))
    print(json.dumps({'pending_products':len(report['candidates']),'changed_products':report['changed_products'],'changed_fields':report['changed_fields']}))
