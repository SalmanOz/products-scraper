"""Selected, applied public fixed basket promotions; never coupons or member deals."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re


PROOF_KEYS = {'version', 'id', 'name', 'discount_type', 'promotion_type', 'is_applied',
              'is_limit_satisfied', 'is_only_az', 'is_ty_plus', 'moc_per_user',
              'discount_amount', 'promotion_end_date', 'valid_until'}
OBSERVED_KEYS = ('id', 'name', 'discountType', 'promotionDiscountType', 'isApplied',
                 'isLimitSatisfied', 'isOnlyAz', 'isTyPlus', 'mocPerUser', 'promotionEndDate')
SOURCE_KEYS = set(OBSERVED_KEYS) | {'promotionRemainingDays', 'promotionRemainingHours',
                                    'promotionRemainingMinutes', 'score', 'buyBoxScore'}


def amount(raw):
    if isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
        return value if value.is_finite() and 0 < value <= 1000000 and value * 100 == (value * 100).to_integral_value() else None
    except (ValueError, InvalidOperation):
        return None


def cash(raw):
    if not isinstance(raw, str):
        return None
    match = re.fullmatch(r'((?:\d{1,7}|\d{1,3}(?:\.\d{3})+)(?:,\d{1,2})?)\s*(?:TL|₺)', raw.strip())
    return amount(match[1].replace('.', '').replace(',', '.')) if match else None


def validity_deadline(raw):
    if not isinstance(raw, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', raw):
        return None
    try:
        end = datetime.fromisoformat(raw)
        # The public date has no UTC offset. End our proof on the preceding
        # UTC day rather than guessing the retailer's timezone or keeping a
        # known expiring campaign alive for the ordinary 72-hour price window.
        return datetime.combine(end.date() - timedelta(days=1), datetime.min.time(), timezone.utc)
    except (ValueError, OverflowError):
        return None


def valid_basket_promotion(proof, listed, payable):
    if not isinstance(proof, dict) or set(proof) != PROOF_KEYS:
        return False
    deadline = validity_deadline(proof.get('promotion_end_date'))
    discount = amount(proof.get('discount_amount'))
    listed, payable = amount(listed), amount(payable)
    label = re.fullmatch(r'Sepette ((?:\d{1,7}|\d{1,3}(?:\.\d{3})+)(?:,\d{1,2})?) TL İndirim', str(proof.get('name') or ''))
    return (proof.get('version') == 'trendyol-public-fixed-basket-v1'
            and isinstance(proof.get('id'), str) and re.fullmatch(r'[1-9]\d{0,19}', proof['id']) is not None
            and type(proof.get('discount_type')) is int and proof['discount_type'] == 2
            and proof.get('promotion_type') == 'DiscountOnBasket'
            and proof.get('is_applied') is True and proof.get('is_limit_satisfied') is False
            and proof.get('is_only_az') is False and proof.get('is_ty_plus') is False
            and type(proof.get('moc_per_user')) is int and proof['moc_per_user'] == 0
            and listed is not None and payable is not None and payable < listed
            and discount is not None and discount == listed - payable
            and label is not None and cash(label[1] + ' TL') == discount
            and deadline is not None and proof.get('valid_until') == deadline.isoformat().replace('+00:00', 'Z'))


def public_basket_price(soup, listing, winner):
    blocks = soup.select('.campaign-price-content')
    roots = soup.select('div.price.campaign-price.campaign-price--variant-common-price')
    if (len(blocks) != 1 or len(roots) != 1 or roots[0].select('.campaign-price-content') != blocks
            or soup.select('[data-testid="normal-price"], [data-testid="lowest-price"]')):
        return None
    prices = winner.get('price')
    if (not isinstance(prices, dict) or prices.get('currency') != 'TRY'
            or type(winner.get('maxSaleLimit')) is not int or winner['maxSaleLimit'] < 1):
        return None
    quoted = {}
    for key in ('sellingPrice', 'discountedPrice', 'couponApplicablePrice',
                'tyPlusCouponApplicablePrice', 'discountedPriceAfterNoLimitPromotions'):
        value = prices.get(key)
        number = amount(value.get('value')) if isinstance(value, dict) else None
        if number is None or cash(value.get('text')) != number:
            return None
        quoted[key] = number
    listed, payable = quoted['sellingPrice'], quoted['discountedPrice']
    if payable >= listed or any(value != payable for key, value in quoted.items() if key != 'sellingPrice'):
        return None
    block = blocks[0]
    values = (block.select('.campaign-price-wrapper > .text'), block.select('.new-price'), block.select('.old-price'))
    if any(len(items) != 1 for items in values):
        return None
    if (values[0][0].get_text(' ', strip=True) != 'Sepette'
            or cash(values[1][0].get_text(' ', strip=True)) != payable
            or cash(values[2][0].get_text(' ', strip=True)) != listed):
        return None
    promotions, filtered = listing.get('promotions'), listing.get('filteredPromotions')
    if (not isinstance(promotions, list) or not 1 <= len(promotions) <= 10
            or not isinstance(filtered, list) or len(filtered) != 1 or not isinstance(filtered[0], dict)
            or any(not isinstance(row, dict) for row in promotions)):
        return None
    money = [row for row in promotions if row.get('promotionDiscountType') != 'Cargo']
    if (len(money) != 1 or type(money[0].get('id')) is not int or money[0]['id'] <= 0
            or set(money[0]) - SOURCE_KEYS or set(filtered[0]) - SOURCE_KEYS
            or any(key not in money[0] or key not in filtered[0]
                   or type(money[0][key]) is not type(filtered[0][key])
                   or money[0][key] != filtered[0][key] for key in OBSERVED_KEYS)):
        return None
    row = money[0]
    proof = {'version': 'trendyol-public-fixed-basket-v1', 'id': str(row['id']), 'name': row.get('name'),
             'discount_type': row.get('discountType'), 'promotion_type': row.get('promotionDiscountType'),
             'is_applied': row.get('isApplied'), 'is_limit_satisfied': row.get('isLimitSatisfied'),
             'is_only_az': row.get('isOnlyAz'), 'is_ty_plus': row.get('isTyPlus'),
             'moc_per_user': row.get('mocPerUser'), 'discount_amount': float(listed - payable),
             'promotion_end_date': row.get('promotionEndDate')}
    deadline = validity_deadline(proof['promotion_end_date'])
    if deadline is None or deadline <= datetime.now(timezone.utc) + timedelta(minutes=5):
        return None
    proof['valid_until'] = deadline.isoformat().replace('+00:00', 'Z')
    visible = roots[0].select('.campaign-price-info > p.info-text')
    if (len(visible) != 1 or visible[0].get_text(' ', strip=True) != proof['name']
            or not valid_basket_promotion(proof, listed, payable)):
        return None
    return {'price': float(payable), 'listed_price': float(listed), 'basket_promotion': proof}
