"""Repair ONLY reviewed selected class letters. No guesses, score/price changes.

The audit is authenticated GET-only. Public source observations are collected
separately using ordinary access; the apply API preserves old sibling evidence.
"""
import argparse
import json
from pathlib import Path
import re

from source_ingestion import SourceIngestionClient


def expand_reviewed_observations(batch, manifest):
    """Bind explicitly observed letters to the fresh manifest's exact old values.

    No class is derived from the flattened old scale or a drop counter. The
    server still checks model, memory, source URL, age and whole-group CAS.
    """
    if not isinstance(batch, dict) or 'observations' not in batch:
        return batch
    if set(batch) != {'reviewed_on', 'observations'} or not isinstance(batch['observations'], list) or len(batch['observations']) > 100:
        raise ValueError('invalid_reviewed_observations')
    candidates = {p['slug']: p for p in manifest['products']}
    products = []
    required = {'slug', 'name', 'source_url', 'observed_at', 'observed_model',
                'observed_ram_gb', 'observed_storage_gb', 'selected_classes'}
    for observation in batch['observations']:
        if not isinstance(observation, dict) or set(observation) != required:
            raise ValueError('invalid_reviewed_observation')
        selected = observation['selected_classes']
        if not isinstance(selected, dict) or not 1 <= len(selected) <= 3 or set(selected) - {'Repairability', 'Drop resistance', 'Efficiency'}:
            raise ValueError('invalid_selected_classes')
        readings = []
        current = candidates.get(observation['slug'])
        for key, value in selected.items():
            group = 'Batarya' if key == 'Efficiency' else 'Design & Materials'
            field = next((f for f in (current or {}).get('fields', []) if f['group'] == group and f['key'] == key), None)
            if field is None:
                continue  # Already repaired/missing: never overwrite a selected value.
            if observation['source_url'] not in field['source_urls']:
                raise ValueError('reviewed_source_not_in_manifest')
            readings.append({'group': group, 'key': key, 'expected_value': field['value'], 'value': value,
                             **{k: observation[k] for k in required - {'slug', 'name', 'selected_classes'}}})
        products.append({'slug': observation['slug'], 'name': observation['name'], 'readings': readings})
    return {'reviewed_on': batch['reviewed_on'], 'products': products}


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
    batch=expand_reviewed_observations(batch,manifest)
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
        if readings==[]:
            result['reason']='no_remaining_reviewed_fields';save();continue
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
