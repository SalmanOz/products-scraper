"""Read allowlisted public documents through the TeknoSkor collection endpoint."""

from datetime import datetime, timezone
import os
from urllib.parse import urlsplit

import requests

from offer_ingestion_client import IngestionClientError, OfferIngestionClient, PublicDocumentResponse
from priority_retailer_transport import (
    MAX_HTML_BYTES, n11_canonical_redirect, n11_unscoped_redirect, validated_n11_redirect_diagnostic,
)


def catalog_document_reader(scope):
    token = os.getenv('CATALOG_SOURCE_OIDC')
    if not token:
        return None
    client = OfferIngestionClient('https://teknoskor.com', token, timeout=(5, 30), max_attempts=1)

    def read(url, **_options):
        try:
            body = client._request_json(
                'POST', f'/api/ingestion/catalog-source-document?scope={scope}',
                headers={'Authorization': 'Bearer ' + client.secret, 'Accept': 'application/json'},
                json={'url': url}, allow_redirects=False,
            )
            route = urlsplit(url)
            n11 = route.hostname == 'www.n11.com'
            purpose = 'public_n11_document' if n11 else 'public_catalog_source_document'
            stamp = datetime.fromisoformat(body['observed_at'].replace('Z', '+00:00'))
            maximum = 64 * 1024 if route.path == '/robots.txt' else MAX_HTML_BYTES
            status, document, content_type = body['source_status'], body['document'], body['content_type']
            if (body.get('purpose') != purpose or body.get('writes') is not False or body.get('url') != url
                    or type(status) is not int or not 100 <= status <= 599 or not isinstance(document, str)
                    or len(document.encode('utf-8')) > maximum or not isinstance(content_type, str)
                    or len(content_type) > 200 or stamp.tzinfo is None
                    or not -60 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 60):
                raise ValueError('Unbound or stale catalog document')
            redirect = body.get('redirect_url')
            unscoped = body.get('unscoped_redirect_url')
            if redirect is not None or unscoped is not None:
                if (not n11 or status not in (301, 302, 307, 308) or redirect and unscoped
                        or redirect and not n11_canonical_redirect(url, redirect)
                        or unscoped and not n11_unscoped_redirect(url, unscoped)):
                    raise ValueError('Unbound retailer redirect')
            diagnostic = validated_n11_redirect_diagnostic(body.get('redirect_diagnostic')) if n11 else None
            return PublicDocumentResponse(url, status, document, content_type, redirect or unscoped, diagnostic)
        except IngestionClientError:
            return PublicDocumentResponse(url, 503, '', 'text/plain')
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise requests.RequestException('Invalid catalog source document response') from error

    return read
