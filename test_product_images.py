from bs4 import BeautifulSoup

from product_images import extract_source_image_urls


def test_extracts_and_deduplicates_only_product_images():
    soup = BeautifulSoup(
        """
        <div class="item-gallery">
          <img data-src="//img.example/phone_x_small.jpg">
          <img src="https://img.example/logo.png">
          <img src="https://img.example/phone_x_small.jpg">
        </div>
        """,
        "html.parser",
    )
    urls = extract_source_image_urls(
        soup,
        {"image": "https://img.example/phone_main.jpg"},
    )
    assert urls == [
        "https://img.example/phone_big.jpg",
        "https://img.example/phone_main.jpg",
    ]


if __name__ == "__main__":
    test_extracts_and_deduplicates_only_product_images()
    print("product image tests passed")
