from unittest.mock import patch

from catalog_expansion import expand_catalog


class FakeResponse:
    status_code = 200


class FakeClient:
    base_url = "https://teknoskor.com"

    def __init__(self):
        self.calls = []

    def _request_with_retry(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse()

    @staticmethod
    def _json_body(_response):
        return {
            "committed": True,
            "created": 1,
            "existing": 0,
            "products": [{"slug": "phone-one", "outcome": "created"}],
        }


def test_reviewed_batch_is_sent_without_arbitrary_product_payload():
    client = FakeClient()
    with patch("catalog_expansion.SourceIngestionClient.from_env", return_value=client):
        result = expand_catalog("reviewed-v1")

    assert result["committed"] is True
    assert client.calls[0][2]["json"] == {"batch_id": "reviewed-v1"}


if __name__ == "__main__":
    test_reviewed_batch_is_sent_without_arbitrary_product_payload()
    print("catalog expansion tests passed")
