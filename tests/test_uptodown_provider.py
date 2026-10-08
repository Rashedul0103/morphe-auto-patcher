import unittest

from apk_providers import DEFAULT_PROVIDER_URLS, UptodownProvider


class UptodownProviderTests(unittest.TestCase):
    def test_download_button_token_is_used(self):
        provider = UptodownProvider()
        html = """
        <html><body>
          <a data-url="apps">unrelated</a>
          <a id="detail-download-button" data-url="real-token-123"></a>
        </body></html>
        """
        self.assertEqual(
            provider._extract_direct_from_body(html),
            "https://dw.uptodown.com/dwn/real-token-123",
        )

    def test_unrelated_data_url_is_not_a_download(self):
        provider = UptodownProvider()
        html = '<a data-url="apps">unrelated navigation</a>'
        self.assertEqual(provider._extract_direct_from_body(html), "")

    def test_no_app_specific_provider_defaults(self):
        self.assertEqual(DEFAULT_PROVIDER_URLS, {})


if __name__ == "__main__":
    unittest.main()
