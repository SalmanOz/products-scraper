import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urljoin, urlparse

import requests


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
MAX_INGESTION_BATCH_SIZE = 500
CATALOG_PAGE_SIZE = 500
COMPLETE_TARGET_PAGE_SIZE = 12
MAX_COMPLETE_TARGET_PAGES = 5
TARGET_INVALID_REASONS = (
    'missing_or_oversize_url', 'malformed_url', 'non_https_url',
    'unsupported_host', 'credentials_or_port', 'retailer_query_restricted',
    'sensitive_query', 'missing_product_path', 'seller_identity',
)


class IngestionClientError(RuntimeError):
    pass


class PublicDocumentResponse:
    def __init__(self, url, status, document, content_type, location=None, redirect_diagnostic=None):
        self.url, self.status_code = url, status
        self.redirect_diagnostic = redirect_diagnostic
        self._body = document.encode('utf-8')
        self.headers = {'Content-Type': content_type, 'Content-Length': str(len(self._body))}
        if location is not None:
            self.headers['Location'] = location

    def iter_content(self, chunk_size=65536):
        for offset in range(0, len(self._body), chunk_size):
            yield self._body[offset:offset + chunk_size]

    def close(self):
        self._body = b''


class OfferIngestionClient:
    def __init__(
        self,
        base_url,
        secret,
        session=None,
        timeout=(10, 120),
        max_attempts=4,
        sleep=time.sleep,
    ):
        normalized_base_url = str(base_url or "").strip().rstrip("/")
        parsed = urlparse(normalized_base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise IngestionClientError(
                "TEKNOSKOR_INGESTION_URL must be an https:// base URL"
            )

        normalized_secret = str(secret or "").strip()
        if len(normalized_secret) < 32:
            raise IngestionClientError(
                "SCRAPER_INGESTION_SECRET must contain at least 32 characters"
            )

        self.base_url = normalized_base_url
        self.secret = normalized_secret
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_attempts = max(1, int(max_attempts))
        self.sleep = sleep

    @classmethod
    def from_env(cls):
        return cls(
            os.getenv("TEKNOSKOR_INGESTION_URL"),
            os.getenv("SCRAPER_INGESTION_SECRET"),
        )

    def _url(self, path):
        return urljoin(f"{self.base_url}/", path.lstrip("/"))

    @staticmethod
    def _json_body(response):
        try:
            return response.json()
        except (TypeError, ValueError) as error:
            raise IngestionClientError(
                f"HTTP {response.status_code} returned invalid JSON"
            ) from error

    def _request_json(self, method, path, **kwargs):
        url = self._url(path)
        last_error = None
        scoring_retry_pending = False

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=self.timeout,
                    **kwargs,
                )
            except requests.RequestException as error:
                last_error = error
                if attempt == self.max_attempts:
                    break
                delay = 2 ** (attempt - 1)
                logging.warning(
                    "  ⚠️ HTTPS request failed (%s/%s): %s; retrying in %ss",
                    attempt,
                    self.max_attempts,
                    error,
                    delay,
                )
                self.sleep(delay)
                continue

            try:
                body = self._json_body(response)
            except IngestionClientError as error:
                last_error = error
                if (
                    response.status_code not in RETRYABLE_STATUS_CODES
                    or attempt == self.max_attempts
                ):
                    raise
                delay = 2 ** (attempt - 1)
                logging.warning(
                    "  ⚠️ HTTPS endpoint returned invalid JSON with HTTP %s "
                    "(%s/%s); retrying in %ss",
                    response.status_code,
                    attempt,
                    self.max_attempts,
                    delay,
                )
                self.sleep(delay)
                continue

            if (
                method.upper() == "POST"
                and isinstance(body, dict)
                and body.get("committed") is True
            ):
                # The rows are durable, but price/performance lists remain
                # stale until the score recalculation succeeds. Replaying the
                # exact observation is idempotent and gives that follow-up
                # work another bounded chance to complete.
                if body.get("scores_recalculated") is False or (
                    scoring_retry_pending and body.get("scores_recalculated") is not True
                ):
                    scoring_retry_pending = True
                    last_error = IngestionClientError(
                        "Offer batch was committed, but score recalculation "
                        f"failed with HTTP {response.status_code}"
                    )
                    if (
                        response.status_code in RETRYABLE_STATUS_CODES
                        and attempt < self.max_attempts
                    ):
                        delay = 2 ** (attempt - 1)
                        logging.warning(
                            "  ⚠️ Offer batch committed, but score "
                            "recalculation failed (%s/%s); retrying the "
                            "idempotent batch in %ss",
                            attempt,
                            self.max_attempts,
                            delay,
                        )
                        self.sleep(delay)
                        continue
                    raise IngestionClientError(
                        "Offer batch was committed, but score recalculation "
                        f"failed after {attempt} attempt(s)"
                    )
                return body

            if 200 <= response.status_code < 300:
                return body

            message = body.get("error") if isinstance(body, dict) else None
            last_error = IngestionClientError(
                f"HTTP {response.status_code} from {path}: "
                f"{message or 'request failed'}"
            )
            if (
                response.status_code not in RETRYABLE_STATUS_CODES
                or attempt == self.max_attempts
            ):
                raise last_error

            delay = 2 ** (attempt - 1)
            logging.warning(
                "  ⚠️ HTTPS endpoint returned HTTP %s (%s/%s); "
                "retrying in %ss",
                response.status_code,
                attempt,
                self.max_attempts,
                delay,
            )
            self.sleep(delay)

        raise IngestionClientError(
            f"HTTPS request to {path} failed after "
            f"{self.max_attempts} attempts: {last_error}"
        )

    def get_published_products(self):
        products = []
        seen_ids = set()
        page = 1
        headers = {
            "Authorization": f"Bearer {self.secret}",
            "Accept": "application/json",
        }

        while True:
            body = self._request_json(
                "GET",
                "/api/ingestion/catalog",
                headers=headers,
                params={
                    "page": str(page),
                    "limit": str(CATALOG_PAGE_SIZE),
                    "include_offer_targets": "1",
                },
            )
            if not isinstance(body, dict) or not isinstance(
                body.get("products"), list
            ):
                raise IngestionClientError(
                    "Catalog endpoint returned an unexpected response"
                )

            for raw_product in body["products"]:
                try:
                    product_id = int(raw_product["id"])
                except (KeyError, TypeError, ValueError) as error:
                    raise IngestionClientError(
                        "Catalog product is missing a valid id"
                    ) from error

                name = str(raw_product.get("name") or "").strip()
                slug = str(raw_product.get("slug") or "").strip()
                if product_id <= 0 or not name or not slug:
                    raise IngestionClientError(
                        f"Catalog product {product_id} is missing name or slug"
                    )
                if product_id in seen_ids:
                    raise IngestionClientError(
                        f"Catalog endpoint returned duplicate product id {product_id}"
                    )

                attributes = raw_product.get("attributes")
                targets = raw_product.get("offer_targets")
                truncated = raw_product.get("offer_targets_truncated")
                if ("offer_targets_truncated" in raw_product and
                        type(truncated) is not bool):
                    raise IngestionClientError(
                        f"Catalog product {product_id} has invalid target completeness"
                    )
                if truncated is not None and not isinstance(targets, list):
                    raise IngestionClientError(
                        f"Catalog product {product_id} has invalid offer targets"
                    )
                targets = targets if isinstance(targets, list) else []
                # Refresh hints, not reusable prices or SKU proof. The fetcher
                # validates each destination again before making a request.
                offer_targets = [
                    {key: target.get(key) for key in (
                        "merchant_name", "source_url", "checked_at",
                    )}
                    | ({"seller_name": target["seller_name"]}
                       if "seller_name" in target else {})
                    for target in targets[:12]
                    if isinstance(target, dict)
                    and isinstance(target.get("source_url"), str)
                    and len(target["source_url"]) <= 1000
                ]
                # Filtering or the local cap can only make completeness worse.
                if len(offer_targets) != len(targets):
                    truncated = True
                # Existing provenance URLs route fresh reads, never stored facts.
                technical_hints = raw_product.get('technical_source_urls')
                technical_urls = []
                if isinstance(technical_hints, list) and len(technical_hints) <= 4:
                    for hint in technical_hints:
                        if (isinstance(hint, str) and len(hint) <= 2048
                                and re.fullmatch(r'https://www\.kimovil\.com/en/where-to-buy-[a-z0-9-]+', hint)
                                and hint not in technical_urls):
                            technical_urls.append(hint)
                products.append(
                    {
                        "id": product_id,
                        "name": name,
                        "slug": slug,
                        "attributes": (
                            attributes if isinstance(attributes, dict) else {}
                        ),
                        "offer_targets": offer_targets,
                        "offer_targets_truncated": truncated,
                        **({'technical_source_urls': technical_urls} if technical_urls else {}),
                    }
                )
                seen_ids.add(product_id)

            has_more = body.get("hasMore") is True
            if not has_more:
                break
            page += 1

        if not products:
            raise IngestionClientError(
                "Catalog endpoint returned no published products"
            )
        return products

    def get_complete_offer_targets(self, product_id):
        """Read every safe target for one product with bounded stable pages.

        This is only extra catalog evidence. A missing or changing page, a
        discarded URL or a duplicate target fails closed; no price is read
        from these records.
        """
        if type(product_id) is not int or product_id <= 0:
            raise IngestionClientError('Complete target product id is invalid')
        headers = {'Authorization': f'Bearer {self.secret}',
                   'Accept': 'application/json'}
        first_identity = None
        first_metadata = None
        gathered = []
        seen_urls = set()
        for page in range(1, MAX_COMPLETE_TARGET_PAGES + 1):
            body = self._request_json(
                'GET', '/api/ingestion/catalog', headers=headers,
                params={'include_offer_targets': '1',
                        'offer_target_product_id': str(product_id),
                        'offer_target_page': str(page)},
            )
            rows = body.get('products') if isinstance(body, dict) else None
            if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
                raise IngestionClientError('Complete target product is unavailable')
            product = rows[0]
            identity = (product.get('id'), product.get('slug'), product.get('name'))
            if (type(identity[0]) is not int or identity[0] != product_id
                    or not isinstance(identity[1], str) or not identity[1]
                    or not isinstance(identity[2], str) or not identity[2]):
                raise IngestionClientError('Complete target product identity changed')
            if first_identity is None:
                first_identity = identity
            elif identity != first_identity:
                raise IngestionClientError('Complete target product identity changed')
            total = product.get('offer_targets_total')
            invalid = product.get('offer_targets_invalid_count')
            duplicate = product.get('offer_targets_duplicate_count')
            response_page = product.get('offer_targets_page')
            snapshot = product.get('offer_targets_snapshot')
            truncated = product.get('offer_targets_truncated')
            if (type(total) is not int or not 0 <= total <=
                    COMPLETE_TARGET_PAGE_SIZE * MAX_COMPLETE_TARGET_PAGES
                    or type(invalid) is not int or invalid < 0
                    or type(duplicate) is not int or duplicate < 0
                    or type(response_page) is not int or response_page != page
                    or type(truncated) is not bool
                    or not isinstance(snapshot, str)
                    or not re.fullmatch(r'[0-9a-f]{64}', snapshot)):
                raise IngestionClientError('Complete target page metadata is invalid')
            if invalid:
                raise IngestionClientError(
                    f'Complete target source has invalid rows '
                    f'(total={total}, invalid={invalid}, duplicate={duplicate})')
            if duplicate:
                raise IngestionClientError(
                    f'Complete target source has duplicate rows '
                    f'(total={total}, invalid={invalid}, duplicate={duplicate})')
            if truncated is not (page * COMPLETE_TARGET_PAGE_SIZE < total):
                raise IngestionClientError('Complete target page completeness is invalid')
            metadata = (total, invalid, duplicate, snapshot)
            if first_metadata is None:
                first_metadata = metadata
            elif metadata != first_metadata:
                raise IngestionClientError('Complete target snapshot changed')
            targets = product.get('offer_targets')
            expected_length = min(COMPLETE_TARGET_PAGE_SIZE,
                                  max(0, total - len(gathered)))
            if not isinstance(targets, list) or len(targets) != expected_length:
                raise IngestionClientError('Complete target page is missing rows')
            for target in targets:
                if not isinstance(target, dict):
                    raise IngestionClientError('Complete target row is invalid')
                url = target.get('source_url')
                merchant = target.get('merchant_name')
                seller = target.get('seller_name') if 'seller_name' in target else merchant
                try:
                    parsed = urlparse(url) if isinstance(url, str) else None
                    valid_url = bool(parsed and parsed.scheme == 'https' and
                                     parsed.hostname and not parsed.port and
                                     not parsed.username and not parsed.password and
                                     parsed.path not in ('', '/') and
                                     not parsed.fragment and
                                     not any(re.search(
                                         r'(?:token|auth|password|secret|signature|session|email|api.?key|access.?key|credential|add.?to.?cart|wc.?ajax|nonce|pao.?key|pao.?edit)',
                                         key, re.I)
                                         for key, _ in parse_qsl(parsed.query,
                                                                max_num_fields=50)))
                except ValueError:
                    valid_url = False
                if (not isinstance(url, str) or len(url) > 1000 or
                        not valid_url or
                        not isinstance(merchant, str) or not merchant.strip() or
                        not isinstance(seller, str) or not seller.strip() or
                        target.get('checked_at') is not None and
                        not isinstance(target.get('checked_at'), str)):
                    raise IngestionClientError('Complete target row is invalid')
                if url in seen_urls:
                    raise IngestionClientError('Complete target URL repeated across pages')
                seen_urls.add(url)
                gathered.append({key: target.get(key) for key in
                                 ('merchant_name', 'source_url', 'checked_at')}
                                | ({'seller_name': target['seller_name']}
                                   if 'seller_name' in target else {}))
            if len(gathered) == total:
                return {'id': first_identity[0], 'slug': first_identity[1],
                        'name': first_identity[2], 'offer_targets': gathered,
                        'offer_targets_truncated': False,
                        'offer_targets_total': total,
                        'offer_targets_snapshot': snapshot}
        raise IngestionClientError('Complete target page limit exceeded')

    def get_offer_target_inspection(self, product_id):
        """Retain the strict single-page contract used by scoped legacy audits."""
        return self._get_offer_target_inspection_page(product_id)

    def get_complete_offer_target_inspection(self, product_id):
        """Read every safe target plus stable aggregate rejects, at most 60 raw rows.

        This is diagnostic data, not a collision clearance. The caller must bind
        the exact snapshot to the independent raw seller proof before writing.
        """
        first, gathered, seen = None, [], set()
        stable = ('id', 'slug', 'name', 'attributes', 'offer_targets_total',
                  'offer_targets_invalid_count', 'offer_targets_duplicate_count',
                  'offer_targets_invalid_reasons', 'offer_targets_snapshot')
        for page in range(1, MAX_COMPLETE_TARGET_PAGES + 1):
            current = self._get_offer_target_inspection_page(product_id, page, multiple=True)
            if first is None:
                first = current
            elif any(first.get(key) != current.get(key) for key in stable):
                raise IngestionClientError('Complete inspection snapshot or product identity changed')
            for target in current['offer_targets']:
                if target['source_url'] in seen:
                    raise IngestionClientError('Complete inspection target repeated across pages')
                seen.add(target['source_url'])
                gathered.append(target)
            if len(gathered) == current['offer_targets_total']:
                result = {**first, 'offer_targets': gathered,
                          'offer_targets_truncated': bool(first['offer_targets_invalid_count']
                                                         or first['offer_targets_duplicate_count'])}
                result.pop('offer_targets_page', None)
                return result
        raise IngestionClientError('Complete inspection page limit exceeded')

    def _get_offer_target_inspection_page(self, product_id, page=1, multiple=False):
        """Read one authenticated diagnostic page, including aggregate rejects.

        The exact one-off Redmi gate only accepts a single full page. Rejected
        rows are counted and categorized by the server, never returned as URLs.
        """
        if (type(product_id) is not int or product_id <= 0 or type(page) is not int
                or not 1 <= page <= MAX_COMPLETE_TARGET_PAGES):
            raise IngestionClientError('Target inspection product id is invalid')
        body = self._request_json(
            'GET', '/api/ingestion/catalog',
            headers={'Authorization': f'Bearer {self.secret}',
                     'Accept': 'application/json'},
            params={'include_offer_targets': '1',
                    'offer_target_product_id': str(product_id),
                    'offer_target_page': str(page)},
        )
        rows = body.get('products') if isinstance(body, dict) else None
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise IngestionClientError('Target inspection product is unavailable')
        product = rows[0]
        total = product.get('offer_targets_total')
        invalid = product.get('offer_targets_invalid_count')
        duplicate = product.get('offer_targets_duplicate_count')
        reasons = product.get('offer_targets_invalid_reasons')
        snapshot = product.get('offer_targets_snapshot')
        targets = product.get('offer_targets')
        if (type(product.get('id')) is not int or product['id'] != product_id
                or not isinstance(product.get('slug'), str) or not product['slug']
                or not isinstance(product.get('name'), str) or not product['name']
                or type(total) is not int or not 0 <= total <= (60 if multiple else 12)
                or type(invalid) is not int or not 0 <= invalid <= 60
                or type(duplicate) is not int or not 0 <= duplicate <= 60
                or type(product.get('offer_targets_page')) is not int
                or product['offer_targets_page'] != page
                or product.get('offer_targets_truncated') is not
                bool(invalid or duplicate or multiple and page * COMPLETE_TARGET_PAGE_SIZE < total)
                or not isinstance(snapshot, str)
                or not re.fullmatch(r'[0-9a-f]{64}', snapshot)
                or not isinstance(reasons, dict)
                or set(reasons) != set(TARGET_INVALID_REASONS)
                or any(type(reasons[key]) is not int or reasons[key] < 0
                       for key in TARGET_INVALID_REASONS)
                or sum(reasons.values()) != invalid
                or not isinstance(targets, list) or len(targets) != (
                    min(COMPLETE_TARGET_PAGE_SIZE, max(0, total - (page - 1) * COMPLETE_TARGET_PAGE_SIZE))
                    if multiple else total)
                or multiple and total + invalid + duplicate > 60):
            raise IngestionClientError('Target inspection metadata is invalid')
        seen_urls = set()
        safe_targets = []
        for target in targets:
            if not isinstance(target, dict):
                raise IngestionClientError('Target inspection row is invalid')
            url = target.get('source_url')
            merchant = target.get('merchant_name')
            seller = target.get('seller_name') if 'seller_name' in target else merchant
            try:
                parsed = urlparse(url) if isinstance(url, str) else None
                valid_url = bool(parsed and parsed.scheme == 'https' and
                                 parsed.hostname and not parsed.port and
                                 not parsed.username and not parsed.password and
                                 parsed.path not in ('', '/') and not parsed.fragment and
                                 not any(re.search(
                                     r'(?:token|auth|password|secret|signature|session|email|api.?key|access.?key|credential|add.?to.?cart|wc.?ajax|nonce|pao.?key|pao.?edit)',
                                     key, re.I)
                                     for key, _ in parse_qsl(parsed.query,
                                                            max_num_fields=50)))
            except ValueError:
                valid_url = False
            if (not isinstance(url, str) or len(url) > 1000 or not valid_url
                    or url in seen_urls or not isinstance(merchant, str)
                    or not merchant.strip() or not isinstance(seller, str)
                    or not seller.strip() or target.get('checked_at') is not None
                    and not isinstance(target.get('checked_at'), str)):
                raise IngestionClientError('Target inspection row is invalid')
            seen_urls.add(url)
            safe_targets.append({key: target.get(key) for key in
                                 ('merchant_name', 'source_url', 'checked_at')}
                                | ({'seller_name': target['seller_name']}
                                   if 'seller_name' in target else {}))
        return {'id': product_id, 'slug': product['slug'], 'name': product['name'],
                'attributes': product.get('attributes'),
                'offer_targets': safe_targets, 'offer_targets_total': total,
                'offer_targets_invalid_count': invalid,
                'offer_targets_duplicate_count': duplicate,
                'offer_targets_invalid_reasons': reasons,
                'offer_targets_truncated': product['offer_targets_truncated'],
                'offer_targets_snapshot': snapshot,
                **({'offer_targets_page': page} if multiple else {})}

    def get_raw_seller_collision_proof(self, product_id, seller_name, source_url):
        """Read a separate authenticated proof over raw stored seller/URL rows.

        The endpoint returns only booleans and counts. Historical URLs are
        never used as refresh hints here, and this proof does not clear the
        existing complete-target ingestion gate.
        """
        if (type(product_id) is not int or product_id <= 0 or
                not isinstance(seller_name, str) or not seller_name.strip() or
                len(seller_name) > 100 or not isinstance(source_url, str) or
                len(source_url) > 500):
            raise IngestionClientError('Raw seller collision request is invalid')
        body = self._request_json(
            'GET', '/api/ingestion/offer-target-collision',
            headers={'Authorization': f'Bearer {self.secret}',
                     'Accept': 'application/json'},
            params={'product_id': str(product_id),
                    'seller_name': seller_name, 'source_url': source_url},
        )
        fields = ('stored_rows_checked', 'unknown_seller_rows', 'invalid_url_rows',
                  'invalid_url_other_seller_rows', 'invalid_url_proposed_seller_rows',
                  'proposed_seller_other_url_rows', 'exact_seller_url_rows',
                  'invalid_row_identity_rows')
        refresh_fields = ('refresh_targets_total', 'refresh_targets_invalid_count',
                          'refresh_targets_duplicate_count')
        refresh_reasons = body.get('refresh_targets_invalid_reasons') if isinstance(body, dict) else None
        if (not isinstance(body, dict) or
                body.get('purpose') != 'read_only_raw_seller_collision_proof' or
                body.get('writes') is not False or
                type(body.get('product_id')) is not int or
                body['product_id'] != product_id or
                type(body.get('complete')) is not bool or
                type(body.get('blocked')) is not bool or
                any(type(body.get(key)) is not int or body[key] < 0 for key in fields) or
                any(type(body.get(key)) is not int or body[key] < 0
                    for key in refresh_fields) or
                not isinstance(refresh_reasons, dict) or
                set(refresh_reasons) != set(TARGET_INVALID_REASONS) or
                any(type(refresh_reasons[key]) is not int or refresh_reasons[key] < 0
                    for key in TARGET_INVALID_REASONS) or
                sum(refresh_reasons.values()) != body['refresh_targets_invalid_count'] or
                not isinstance(body.get('refresh_targets_snapshot'), str) or
                not re.fullmatch(r'[0-9a-f]{64}', body['refresh_targets_snapshot']) or
                sum(body[key] for key in refresh_fields) != body['stored_rows_checked'] or
                body['stored_rows_checked'] > 61 or
                body['complete'] != (body['stored_rows_checked'] <= 60) or
                body['unknown_seller_rows'] > body['stored_rows_checked'] or
                body['invalid_url_rows'] > body['stored_rows_checked'] or
                body['invalid_url_other_seller_rows'] +
                body['invalid_url_proposed_seller_rows'] > body['invalid_url_rows'] or
                body['exact_seller_url_rows'] +
                body['proposed_seller_other_url_rows'] > body['stored_rows_checked'] or
                not isinstance(body.get('reasons'), list)):
            raise IngestionClientError('Raw seller collision proof is invalid')
        reasons = [
            *(['stored_row_limit_exceeded'] if not body['complete'] else []),
            *(['invalid_stored_row_identity'] if body['invalid_row_identity_rows'] else []),
            *(['unknown_seller_identity'] if body['unknown_seller_rows'] else []),
            *(['proposed_seller_on_other_url_or_platform']
              if body['proposed_seller_other_url_rows'] else []),
        ]
        if body['reasons'] != reasons or body['blocked'] != bool(reasons):
            raise IngestionClientError('Raw seller collision proof is inconsistent')
        return body

    def get_public_price_priority(self):
        """Read a bounded public catalog snapshot only to order crawl work.

        The returned values are not offers and must never be ingested as prices.
        An absent/mismatched row remains unknown rather than being treated as
        unpriced. Full sync can fall back to its original order on API errors.
        """
        rows = self._request_json(
            "GET", "/api/products", params={"limit": "1000"},
            headers={"Accept": "application/json"},
        )
        if not isinstance(rows, list) or len(rows) > 1000:
            raise IngestionClientError("Public price priority snapshot is invalid")
        status = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                product_id = int(row['id'])
                slug = row['slug']
                if (product_id <= 0 or product_id in status
                        or not isinstance(slug, str) or not slug
                        or 'fresh_lowest_price' not in row
                        or 'fresh_offer_count' not in row):
                    continue
                price = float(row['fresh_lowest_price'] or 0)
                offer_count = int(row['fresh_offer_count'] or 0)
                if not math.isfinite(price) or price < 0 or offer_count < 0:
                    continue
                status[product_id] = (slug, price > 0 and offer_count > 0)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        return status

    def resolve_catalog_radio(self, product, observation, *, publish=False):
        """Dedicated missing-only manufacturer contract; never the generic source writer."""
        contract = 'manufacturer-catalog-radio-v1'
        payload = {'contract': contract, 'mode': 'publish' if publish else 'validate',
                   'product': {key: product[key] for key in ('id', 'slug', 'name')},
                   'observation': observation}
        body = self._request_json('POST', '/api/ingestion/catalog-radio', json=payload,
            headers={'Authorization': f'Bearer {self.secret}', 'Accept': 'application/json'})
        generation = '5g' if '5g' in observation['network_technologies'] else '4g'
        if (not isinstance(body, dict) or body.get('contract') != contract
                or body.get('network_generation') != generation):
            raise IngestionClientError('Manufacturer radio contract not confirmed')
        if not publish:
            if body.get('eligible') is not True or body.get('committed') is not False or body.get('writes') is not False:
                raise IngestionClientError('Manufacturer radio validation was not read-only')
        elif (body.get('committed') is not True or type(body.get('product_id')) is not int or body.get('product_id') != product['id']
                or body.get('product_slug') != product['slug'] or body.get('ram_storage_unchanged') is not True
                or body.get('scores_recalculated') is not True or body.get('outcome') not in ('resolved', 'already_current')):
            raise IngestionClientError('Manufacturer radio publication not fully confirmed')
        return body

    def get_offer_identity_readiness(self):
        """Authenticated, current commercial-offer identity readback."""
        body = self._request_json(
            "GET", "/api/ingestion/offer-identity-readiness",
            headers={"Authorization": f"Bearer {self.secret}",
                     "Accept": "application/json"},
        )
        identity = body.get('offerIdentity') if isinstance(body, dict) else None
        if (not isinstance(identity, dict)
                or identity.get('evidenceVersion') != 'product-offer-match-v1'
                or identity.get('mode') != 'shadow_audit_only'
                or not isinstance(identity.get('productCoverage'), list)):
            raise IngestionClientError('Offer identity readiness response is invalid')
        return identity

    def n11_document_get(self, url, **_transport_options):
        """Read a public document from the website's dedicated collector.

        The ingestion credential goes only to our existing application host;
        the collector never forwards it or visitor cookies to the retailer.
        """
        from priority_retailer_transport import (n11_product_selectors, n11_canonical_redirect,
                                                n11_unscoped_redirect, validated_n11_redirect_diagnostic,
                                                N11_PHONE_SEARCH_PATH)
        parsed = urlparse(url)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        search_key = {'/arama': 'q', N11_PHONE_SEARCH_PATH: 'iw'}.get(parsed.path)
        if (parsed.scheme != 'https' or parsed.netloc != 'www.n11.com' or parsed.fragment
                or not (n11_product_selectors(url) is not None
                        or parsed.path == '/robots.txt' and not parsed.query
                        or search_key is not None and len(query) == 1 and query[0][0] == search_key
                        and 1 <= len(query[0][1]) <= 180 and not re.search(r'[\x00-\x1f\x7f]', query[0][1]))):
            raise requests.RequestException('Invalid public n11 collector destination')
        try:
            body = self._request_json('POST', '/api/ingestion/n11-document',
                headers={'Authorization': f'Bearer {self.secret}', 'Accept': 'application/json'},
                json={'url': url})
            stamp = datetime.fromisoformat(body['observed_at'].replace('Z', '+00:00'))
            maximum = 64 * 1024 if parsed.path == '/robots.txt' else 5 * 1024 * 1024
            if (body.get('purpose') != 'public_n11_document' or body.get('writes') is not False
                    or body.get('url') != url or type(body.get('source_status')) is not int
                    or not 100 <= body['source_status'] <= 599
                    or not isinstance(body.get('document'), str)
                    or len(body['document'].encode('utf-8')) > maximum
                    or not isinstance(body.get('content_type'), str) or len(body['content_type']) > 200
                    or stamp.tzinfo is None
                    or not -60 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 60):
                raise ValueError('Unbound or stale public document response')
            redirect = body.get('redirect_url')
            unscoped = body.get('unscoped_redirect_url')
            if (redirect is not None and (body['source_status'] not in (301, 302, 307, 308)
                    or not n11_canonical_redirect(url, redirect))):
                raise ValueError('Unbound public canonical redirect')
            if (unscoped is not None and (redirect is not None
                    or body['source_status'] not in (301, 302, 307, 308)
                    or not n11_unscoped_redirect(url, unscoped))):
                raise ValueError('Unbound public default PDP redirect')
            diagnostic = (validated_n11_redirect_diagnostic(body.get('redirect_diagnostic'))
                          if body['source_status'] in (301, 302, 307, 308) else None)
            return PublicDocumentResponse(url, body['source_status'], body['document'], body['content_type'],
                                          redirect or unscoped, diagnostic)
        except IngestionClientError:
            # Our collector's temporary gateway/restart failure is not a
            # retailer access denial. The source transport applies its bounded
            # 503 cooldown/probe policy; it never changes destination or client.
            return PublicDocumentResponse(url, 503, '', 'text/plain')
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise requests.RequestException('Dedicated public source collector unavailable') from error

    def ingest_listing_offers(self, offers, *, require_all_accepted=True):
        """Use only the full-evidence, URL-keyed endpoint; never fall back.

        An older deployment has no such route and therefore cannot silently
        apply its old merchant-only update semantics to these observations.
        """
        if require_all_accepted is not True or not offers:
            raise IngestionClientError('Listing ingestion requires a nonempty atomic batch')
        return self.ingest_offers(offers, require_all_accepted=True, _listing_contract=True)

    def ingest_offers(self, offers, *, require_all_accepted=False, _listing_contract=False):
        if type(require_all_accepted) is not bool:
            raise IngestionClientError("require_all_accepted must be a boolean")
        if require_all_accepted and len(offers) > MAX_INGESTION_BATCH_SIZE:
            raise IngestionClientError(
                "Atomic offer ingestion cannot span multiple HTTP batches"
            )
        if not offers:
            return {
                "accepted": 0,
                "stale_ignored": 0,
                "variant_rejected": 0,
                "variant_rejections": [],
                "changed_paths": [],
                "committed": True,
            }

        totals = {
            "accepted": 0,
            "stale_ignored": 0,
            "variant_rejected": 0,
            "variant_rejections": [],
            "changed_paths": [],
            "committed": True,
        }
        headers = {
            "Authorization": f"Bearer {self.secret}",
            "Content-Type": "application/json",
        }
        server_scoring_confirmed = True

        for start in range(0, len(offers), MAX_INGESTION_BATCH_SIZE):
            batch = offers[start : start + MAX_INGESTION_BATCH_SIZE]
            body = self._request_json(
                "POST",
                "/api/ingestion/listing-offers" if _listing_contract else "/api/ingestion/offers",
                headers=headers,
                json={
                    "offers": batch,
                    **({"require_all_accepted": True} if require_all_accepted else {}),
                },
            )
            if not isinstance(body, dict) or body.get("committed") is not True:
                raise IngestionClientError(
                    "Offer ingestion response did not confirm a commit"
                )
            if _listing_contract and body.get('offer_identity_contract') != 'seller-source-url-v1':
                raise IngestionClientError('Listing ingestion did not confirm source URL identity')
            if require_all_accepted and body.get("require_all_accepted") is not True:
                raise IngestionClientError(
                    "Offer ingestion response did not confirm atomic acceptance"
                )

            try:
                accepted = int(body.get("accepted", 0))
                stale_ignored = int(body.get("stale_ignored", 0))
                variant_rejected = int(body.get("variant_rejected", 0))
            except (TypeError, ValueError) as error:
                raise IngestionClientError(
                    "Offer ingestion response has invalid counters"
                ) from error
            if (
                min(accepted, stale_ignored, variant_rejected) < 0
                or accepted + stale_ignored + variant_rejected != len(batch)
            ):
                raise IngestionClientError(
                    "Offer ingestion response count does not match submitted batch"
                )
            if require_all_accepted and (
                accepted != len(batch) or stale_ignored != 0 or variant_rejected != 0
            ):
                raise IngestionClientError(
                    "Atomic offer ingestion did not accept every submitted offer"
                )
            rejections = body.get("variant_rejections", [])
            if not isinstance(rejections, list) or len(rejections) != variant_rejected:
                raise IngestionClientError(
                    "Offer ingestion variant rejection details do not match count"
                )
            seen_indices = set()
            for rejection in rejections:
                index = rejection.get("index") if isinstance(rejection, dict) else None
                if (
                    not isinstance(index, int) or isinstance(index, bool)
                    or index < 0 or index >= len(batch) or index in seen_indices
                    or not isinstance(rejection.get("product_slug"), str)
                    or not rejection["product_slug"].strip()
                    or not isinstance(rejection.get("issues"), list)
                    or not rejection["issues"]
                    or any(not isinstance(issue, str) or not issue.strip()
                           for issue in rejection["issues"])
                ):
                    raise IngestionClientError(
                        "Offer ingestion returned malformed variant rejection details"
                    )
                seen_indices.add(index)
                # Endpoint indices are local to each HTTP batch. Report indices
                # relative to the complete offer list supplied to this call.
                totals["variant_rejections"].append({
                    **rejection, "index": start + index,
                })

            totals["accepted"] += accepted
            totals["stale_ignored"] += stale_ignored
            totals["variant_rejected"] += variant_rejected
            server_scoring_confirmed &= body.get('scores_recalculated') is True
            changed_paths = body.get("changed_paths")
            if isinstance(changed_paths, list):
                totals["changed_paths"].extend(
                    path
                    for path in changed_paths
                    if isinstance(path, str) and path.startswith("/")
                )

        totals["changed_paths"] = list(
            dict.fromkeys(totals["changed_paths"])
        )
        if _listing_contract:
            totals['offer_identity_contract'] = 'seller-source-url-v1'
        if server_scoring_confirmed:
            # Only an explicit success for EVERY committed HTTP batch can
            # replace a redundant reconciliation. Null/missing is unknown.
            totals['server_scores_recalculated'] = True
        return totals

    def reconcile_scores(self):
        """Recompute from existing server evidence; never refresh offer dates."""
        body = self._request_json(
            "POST", "/api/ingestion/scoring/reconcile",
            headers={"Authorization": f"Bearer {self.secret}",
                     "Content-Type": "application/json"},
            json={},
        )
        if not isinstance(body, dict) or body.get("scores_recalculated") is not True:
            raise IngestionClientError("Score reconciliation did not confirm success")
        counters = [body.get(key) for key in ("total", "scored", "skipped")]
        if (any(type(value) is not int or value < 0 for value in counters)
                or counters[0] != counters[1] + counters[2]):
            raise IngestionClientError("Score reconciliation returned invalid counters")
        return body
