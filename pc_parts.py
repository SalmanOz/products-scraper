"""Discover and revalidate GTIN-selected PC parts from a public storefront. Prices are always read live."""
from collections import Counter
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from time import sleep
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
import json
import re
import sys

from bs4 import BeautifulSoup
from PIL import Image, ImageOps
import requests

ROOT='https://www.gaming.gen.tr'
CATEGORIES={'cpu':'islemci','motherboard':'anakart','ram':'ram-bellek','gpu':'ekran-karti','ssd':'ssd',
            'case':'kasa','psu':'guc-kaynagi-psu','cooler':'sogutma-sistemleri'}
if len(sys.argv) not in (2,4) or len(sys.argv)==4 and sys.argv[2]!='--refresh':
    raise SystemExit('Usage: python pc_parts.py OUTPUT_DIR [--refresh EXISTING_SNAPSHOT]')
refresh_source=Path(sys.argv[3]) if len(sys.argv)==4 else None
out=Path(sys.argv[1])
out.mkdir(parents=True,exist_ok=True)
(out/'photos').mkdir(exist_ok=True)
session=requests.Session()
session.headers['User-Agent']='TeknoSkorBot/1.0 (+https://teknoskor.com)'
robots=RobotFileParser()
robots_response=session.get(ROOT+'/robots.txt',timeout=20,allow_redirects=False)
robots_response.raise_for_status()
robots.parse(robots_response.text.splitlines())
rejected=Counter()
class SourceStopped(Exception): pass

def get(url, limit=6_000_000):
    if not robots.can_fetch('TeknoSkorBot',url): raise ValueError('robots_disallowed')
    sleep(.6)
    r=session.get(url,timeout=20,allow_redirects=False)
    if r.status_code in (403,429): raise SourceStopped(f'http_{r.status_code}')
    if r.status_code!=200 or len(r.content)>limit: raise ValueError(f'http_{r.status_code}')
    return r

def gtin(value):
    raw=str(value or '')
    if not raw.isdigit() or len(raw) not in (12,13): return None
    raw=raw.zfill(13)
    total=sum(int(n)*(3 if i%2 else 1) for i,n in enumerate(raw[:-1]))
    return raw if (10-total%10)%10==int(raw[-1]) else None

def nodes(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try: obj=json.loads(script.get_text())
        except ValueError: continue
        if isinstance(obj,dict):
            yield from obj.get('@graph',[obj])

def read(url,kind):
    r=get(url)
    (out/(urlsplit(url).path.split('/')[2]+'.html')).write_text(r.text)
    soup=BeautifulSoup(r.text,'html.parser')
    products=[n for n in nodes(soup) if n.get('@type')=='Product' and n.get('url')==url]
    if len(products)!=1: raise ValueError('selected_product')
    product=products[0]
    heading=soup.select('h1.product_title')
    canonical=soup.select('link[rel="canonical"]')
    cart=soup.select('.summary button.single_add_to_cart_button')
    if (len(heading)!=1 or heading[0].get_text(' ',strip=True)!=product['name']
        or len(canonical)!=1 or canonical[0].get('href')!=url or len(cart)!=1 or cart[0].has_attr('disabled')):
        raise ValueError('heading_canonical_or_cart')
    offers=product.get('offers')
    if not isinstance(offers,list) or len(offers)!=1: raise ValueError('multiple_offers')
    offer=offers[0]
    if (offer.get('priceCurrency')!='TRY' or offer.get('availability')!='https://schema.org/InStock'
        or offer.get('url')!=url or offer.get('seller',{}).get('name')!='Gaming.Gen.TR'):
        raise ValueError('stock_seller_currency')
    specs={}
    for row in soup.select('table.woocommerce-product-attributes tr'):
        k,v=row.find('th'),row.find('td')
        if k and v: specs[k.get_text(' ',strip=True)]=v.get_text(' ',strip=True)
    barcode=gtin(specs.get('EAN'))
    if not barcode or barcode!=gtin(product.get('gtin')) or not specs.get('Üretici Kodu'):
        raise ValueError('gtin_or_mpn')
    current=soup.select('.summary .price ins .amount bdi') or soup.select('.summary .price > .amount bdi')
    price=float(offer['price'])
    if len(current)!=1 or float(re.sub(r'[^\d,]','',current[0].get_text()).replace(',','.'))!=price:
        raise ValueError('visible_price')
    image=product.get('image')
    if not isinstance(image,str) or not image.startswith(ROOT+'/wp-content/uploads/'): raise ValueError('image_source')
    if not refresh_source:
        photo=get(image).content
        im=Image.open(BytesIO(photo))
        im=ImageOps.contain(im.convert('RGB'),(720,720))
        im.save(out/'photos'/(barcode+'.webp'),format='WEBP',quality=85)
    return {'id':barcode,'slug':re.sub(r'[^a-z0-9]+','-',product['name'].lower()).strip('-')[:100]+'-'+barcode,
            'kind':kind,'name':product['name'],'mpn':specs['Üretici Kodu'],'gtin':barcode,
            'brand':specs.get('Marka',product.get('brand',{}).get('name','')),
            'image':'/media/pc-parts/'+barcode+'.webp','specs':specs,'sourceUrl':url,
            'offer':{'price':price,'currency':'TRY','platform':'Gaming.Gen.TR','seller':'Gaming.Gen.TR',
                     'stock':'in_stock','checkedAt':datetime.now(timezone.utc).isoformat(),'url':url}}

if refresh_source:
    previous=json.loads(refresh_source.read_text())
    refreshed=[]
    count=0
    for part in previous['parts']:
        try:
            observed=read(part['sourceUrl'],part['kind'])
            if observed['gtin']!=part['gtin'] or observed['mpn']!=part['mpn'] or observed['specs']!=part['specs']:
                rejected['variant_or_specifications_changed']+=1
                refreshed.append(part)
                continue
            refreshed.append({**part,'offer':observed['offer']})
            count+=1
        except (ValueError,requests.RequestException) as exc:
            rejected[str(exc)[:90]]+=1
            refreshed.append(part)
    result={**previous,'checkedAt':datetime.now(timezone.utc).isoformat(),'parts':refreshed}
    (out/'pcParts.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'refreshed':count,'retained':len(refreshed)-count,'rejected':dict(rejected)},ensure_ascii=False))
    raise SystemExit(0)

parts=[]
try:
    for kind,category in CATEGORIES.items():
        url=ROOT+'/kategori/bilgisayar-bilesenleri/'+category+'/'
        html=get(url).text
        (out/(kind+'-category.html')).write_text(html)
        soup=BeautifulSoup(html,'html.parser')
        links=list(dict.fromkeys(a.get('href') for a in soup.select('ul.products li.product.instock a.woocommerce-LoopProduct-link')
                   if re.fullmatch(ROOT+r'/urun/\d+/[a-z0-9-]+/',a.get('href',''))))
        for url in links[:7]:
            try:
                part=read(url,kind)
                if all(p['id']!=part['id'] for p in parts): parts.append(part)
            except (ValueError,requests.RequestException) as e:
                rejected[str(e)[:90]]+=1
        print(json.dumps({'kind':kind,'discovered':len(links),'accepted':sum(p['kind']==kind for p in parts),'rejected':dict(rejected)},ensure_ascii=False),flush=True)
except SourceStopped as e:
    print(str(e),flush=True)
finally:
    (out/'pcParts.json').write_text(json.dumps({'checkedAt':datetime.now(timezone.utc).isoformat(),'parts':parts},ensure_ascii=False,indent=2)+'\n')
