import unittest
from bs4 import BeautifulSoup

from main import KimovilScraper
from spec_sections import extract_spec_sections, find_section_spec


SOURCE_HTML = """
<html><head><meta name='deviceki' content='{"name":"Phone One"}'></head>
<body>
<section class="container-sheet-hardware"><h2>Performance &amp; Hardware of Phone One</h2>
  <h3>Processor</h3><table class="k-dltable">
    <tr><th>Model</th><td>Chip One</td></tr>
    <tr><th>CPU</th><td>2xXX GHz Super + 4xXX GHz Efficiency</td></tr>
    <tr><th>Type</th><td>Hexa-Core</td></tr>
    <tr><th>Nanometer</th><td>2 nm</td></tr>
  </table>
  <h3>RAM</h3><table class="k-dltable">
    <tr><th>RAM</th><td>12 GB</td></tr><tr><th>Type</th><td>LPDDR5X</td></tr>
  </table>
  <h3>Storage</h3><table class="k-dltable">
    <tr><th>Capacity</th><td>256 GB</td></tr><tr><th>Type</th><td>NVMe</td></tr>
  </table>
</section>
<section class="container-sheet-camera"><h2>Camera of Phone One</h2>
  <h3>Triple rear camera</h3><div><table class="k-dltable">
    <tr><th class="k-head">Standard</th><td class="k-head"><span class="camera-number">1</span></td></tr>
    <tr><th>Resolution</th><td>48 Mpx</td></tr><tr><th>Aperture</th><td>ƒ/ 1.6</td></tr>
  </table></div>
  <dl class="k-dl"><dt class="k-head">Wide Angle + Macro</dt><dd class="k-head"><span class="camera-number">2</span></dd>
    <dt>Resolution</dt><dd>48 Mpx</dd><dt>Aperture</dt><dd>ƒ/ 2.2</dd>
  </dl>
  <table class="k-dltable"><tr><th>Features</th><td><ul><li>OIS</li><li>Autofocus</li></ul></td></tr></table>
  <h3>Selfie</h3><div><table class="k-dltable">
    <tr><th>Resolution</th><td>18 Mpx</td></tr><tr><th>Aperture</th><td>ƒ/ 1.9</td></tr>
  </table></div>
</section>
</body></html>
"""


class SpecSectionTests(unittest.TestCase):
    def scrape(self, html=SOURCE_HTML):
        scraper = KimovilScraper()
        scraper.get_via_flaresolverr = lambda _url: html
        records = []
        self.assertTrue(scraper.scrape_product_details(
            "https://www.kimovil.com/en/where-to-buy-phone-one",
            product_slug="phone-one", expected_name="Phone One",
            record_sink=records,
        ))
        return {record["attribute_key"]: record["value"] for record in records}

    def test_processor_ram_and_storage_types_do_not_overwrite_each_other(self):
        hardware = self.scrape()["Performance & Hardware"]
        self.assertEqual(hardware["Processor Type"], "Hexa-Core")
        self.assertEqual(hardware["RAM Type"], "LPDDR5X")
        self.assertEqual(hardware["Storage Type"], "NVMe")
        self.assertNotIn("Type", hardware)

    def test_rear_selfie_and_additional_lens_specs_remain_distinct(self):
        camera = self.scrape()["Camera"]
        self.assertEqual(camera["Resolution"], "48 Mpx")
        self.assertEqual(camera["Aperture"], "ƒ/ 1.6")
        self.assertEqual(camera["Selfie Resolution"], "18 Mpx")
        self.assertEqual(camera["Selfie Aperture"], "ƒ/ 1.9")
        self.assertEqual(camera["Rear Camera 2 Resolution"], "48 Mpx")
        self.assertEqual(camera["Rear Camera 2 Lens"], "Wide Angle + Macro")
        self.assertEqual(camera["Features"], "OIS; Autofocus")

    def test_unconfirmed_cpu_and_cross_section_capacity_are_not_published(self):
        attributes = self.scrape()
        self.assertNotIn("CPU", attributes["Performance & Hardware"])
        self.assertNotIn("battery_mah", attributes)
        self.assertNotIn("battery", attributes["quick_specs"])
        self.assertEqual(attributes["quick_specs"]["storage"], "256 GB")

    def test_missing_ram_does_not_match_ram_type(self):
        sections = {"Hardware": {"RAM Type": "LPDDR5X", "Capacity": "256 GB"}}
        self.assertEqual(find_section_spec(sections, "Hardware", "Memory RAM", "RAM"), "---")
        self.assertEqual(find_section_spec(sections, "Battery", "Capacity"), "---")

    def test_multiple_selfie_lenses_remain_distinct_and_dxo_is_not_a_physical_spec(self):
        html = SOURCE_HTML.replace('</body>', '''
        <section class="container-sheet-camera"><h2>Camera of Phone One</h2><h3>Selfie</h3>
          <dl class="k-dl"><dt class="k-head">Wide</dt><dd><span class="camera-number">2</span></dd>
            <dt>Resolution</dt><dd>10 Mpx</dd></dl>
          <div class="k-h4">DxOMark Score</div>
          <dl class="k-dl dxomark-scores-dl"><dt>DxOMark Score</dt><dd>172 Camera</dd></dl>
        </section></body>''')
        camera = self.scrape(html)["Camera"]
        self.assertEqual(camera["Selfie Resolution"], "18 Mpx")
        self.assertEqual(camera["Selfie Camera 2 Resolution"], "10 Mpx")
        self.assertFalse(any("DxOMark" in key for key in camera))

    def test_ambiguous_duplicates_are_omitted_instead_of_last_value_winning(self):
        html = '<section class="container-sheet-hardware"><h2>Hardware</h2><table class="k-dltable">'
        html += ''.join(f'<tr><th>Type</th><td>{value}</td></tr>' for value in ("Octa-Core", "NVMe", "LPDDR5"))
        html += '</table></section>'
        specs = extract_spec_sections(BeautifulSoup(html, "html.parser"))
        self.assertNotIn("Type", specs["Hardware"])


if __name__ == "__main__":
    unittest.main()
