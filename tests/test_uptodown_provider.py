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

    def test_api_candidate_rejects_placeholder_download_url(self):
        provider = UptodownProvider()
        candidates = provider._api_candidates(
            "12345",
            {"url": "https://example.en.uptodown.com/android", "packagename": "com.example"},
            [{
                "version": "1.2.3",
                "fileID": "98765",
                "fileType": "xapk",
                "downloadURL": "https://dw.uptodown.com/dwn/apps",
                "architecture": "arm64-v8a",
            }],
            "1.2.3",
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].download_url, "")
        self.assertEqual(candidates[0].details["file_id"], "98765")

    def test_download_prefers_exact_api_endpoint(self):
        from unittest.mock import Mock, patch

        provider = UptodownProvider()
        candidate = provider._api_candidates(
            "12345",
            {"url": "https://example.en.uptodown.com/android", "packagename": "com.example"},
            [{
                "version": "1.2.3",
                "fileID": "98765",
                "fileType": "apk",
                "downloadURL": "https://dw.uptodown.com/dwn/apps",
                "architecture": "arm64-v8a",
            }],
            "1.2.3",
        )[0]

        response = Mock()
        response.status_code = 200
        response.iter_content.return_value = [b"PK" + (b"0" * 2048)]
        response.close = Mock()

        with patch.object(provider, "_api_download_url", return_value="https://dw.uptodown.com/dwn/exact") as api_url,              patch.object(provider, "_direct_from_download_page", side_effect=AssertionError("generic page resolver used")):
            provider.session.get = Mock(return_value=response)
            with patch("builtins.open", create=True):
                import tempfile
                with tempfile.TemporaryDirectory() as tmp:
                    destination = f"{tmp}/artifact.apk"
                    provider.download(candidate, destination)

        api_url.assert_called_once_with("12345", "98765")

    def test_no_app_specific_provider_defaults(self):
        self.assertEqual(DEFAULT_PROVIDER_URLS, {})


if __name__ == "__main__":
    unittest.main()
