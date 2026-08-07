"""
Regression tests for the brochure_link PDF-fallback fix: rule 3 must
produce a genuinely fetchable https:// URL (when GCS-backed storage is
available), not a bare filename with no scheme/host - see
brochure_link_resolver.finalize_brochure_link, storage/blob_store.py
(write_bytes public=True, public_url), and storage/file_store.save_original_pdf.

No network calls, no real GCS - GCS interaction is mocked throughout (this
dev environment has no GCS credentials/bucket configured at all - see the
Cloud Run-only public_url() path). Run with:

    .venv\\Scripts\\python.exe -m unittest tests.test_brochure_link -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brochure_link_resolver import finalize_brochure_link, is_generic_link
from storage import blob_store, file_store


def _fake_bucket():
    blob = MagicMock()
    bucket = MagicMock()
    bucket.blob.return_value = blob
    return bucket, blob


class FinalizeBrochureLinkRule3Tests(unittest.TestCase):
    """The actual bug: rule 3 used to return the bare uploaded filename with
    no scheme/host, which a browser resolves as a relative path on the
    Streamlit app itself (hence 404). It must now return whatever real
    fallback link the caller provides."""

    def test_pdf_with_no_genuine_link_uses_the_provided_fallback_url(self):
        result = finalize_brochure_link(
            None, is_pdf=True, pdf_fallback_link="https://storage.googleapis.com/bucket/brochures/x.pdf"
        )
        self.assertEqual(result, "https://storage.googleapis.com/bucket/brochures/x.pdf")
        self.assertTrue(result.startswith("https://"))

    def test_generic_link_discarded_falls_back_to_provided_url(self):
        # "workplaceplus.co.uk" has no listing-specific path - is_generic_link
        # discards it, same as if nothing had been found at all.
        result = finalize_brochure_link(
            "workplaceplus.co.uk", is_pdf=True, pdf_fallback_link="https://storage.googleapis.com/bucket/x.pdf"
        )
        self.assertEqual(result, "https://storage.googleapis.com/bucket/x.pdf")

    def test_local_dev_mode_still_falls_back_to_bare_filename(self):
        # save_original_pdf returns None with no GCS bucket configured (see
        # SaveOriginalPdfTests below) - extract.py then passes the bare
        # filename through as before, so local dev/CLI usage is unaffected.
        result = finalize_brochure_link(None, is_pdf=True, pdf_fallback_link="Business Cube.pdf")
        self.assertEqual(result, "Business Cube.pdf")

    def test_email_never_falls_back_to_a_link_at_all(self):
        result = finalize_brochure_link(None, is_pdf=False, pdf_fallback_link="ignored.eml")
        self.assertIsNone(result)

    def test_genuine_direct_pdf_link_is_unaffected_by_the_fallback(self):
        # Already a direct .pdf link - resolve_brochure_link returns it as-is
        # with no network fetch (see its own "already a direct document link"
        # short-circuit), so this stays network-free.
        result = finalize_brochure_link(
            "https://example.com/real-brochure.pdf", is_pdf=True, pdf_fallback_link="Business Cube.pdf"
        )
        self.assertEqual(result, "https://example.com/real-brochure.pdf")

    def test_admin_link_discarded_still_falls_back(self):
        result = finalize_brochure_link(
            "https://example.com/unsubscribe?id=1",
            is_pdf=True,
            pdf_fallback_link="https://storage.googleapis.com/bucket/x.pdf",
        )
        self.assertEqual(result, "https://storage.googleapis.com/bucket/x.pdf")


class PublicUrlTests(unittest.TestCase):
    def test_raises_without_a_gcs_bucket_configured(self):
        with patch.object(blob_store, "GCS_BUCKET_NAME", None):
            with self.assertRaises(RuntimeError):
                blob_store.public_url("brochures/x.pdf")

    def test_returns_a_storage_googleapis_https_url(self):
        with patch.object(blob_store, "GCS_BUCKET_NAME", "test-bucket"):
            url = blob_store.public_url("brochures/20260101_000000_x.pdf")
        self.assertEqual(url, "https://storage.googleapis.com/test-bucket/brochures/20260101_000000_x.pdf")
        self.assertTrue(url.startswith("https://"))

    def test_spaces_in_the_object_name_are_percent_encoded(self):
        # A real uploaded filename's stem routinely has spaces (e.g. "Business
        # Cube.pdf") - an un-encoded space in a URL is invalid and would break
        # when clicked.
        with patch.object(blob_store, "GCS_BUCKET_NAME", "test-bucket"):
            url = blob_store.public_url("brochures/20260101_000000_Business Cube.pdf")
        self.assertNotIn(" ", url)
        self.assertIn("Business%20Cube.pdf", url)
        # the "/" pseudo-folder separator must survive encoding, unescaped
        self.assertIn("test-bucket/brochures/", url)


class WriteBytesPublicAclTests(unittest.TestCase):
    def test_public_write_requests_predefined_acl(self):
        bucket, blob = _fake_bucket()
        with patch.object(blob_store, "GCS_BUCKET_NAME", "test-bucket"), \
             patch.object(blob_store, "_get_bucket", return_value=bucket):
            blob_store.write_bytes("brochures/x.pdf", b"PDF DATA", public=True)
        blob.upload_from_string.assert_called_once_with(
            b"PDF DATA", content_type="application/pdf", predefined_acl="publicRead"
        )

    def test_non_public_write_never_requests_an_acl(self):
        # staging/master/versions/log must never become world-readable.
        bucket, blob = _fake_bucket()
        with patch.object(blob_store, "GCS_BUCKET_NAME", "test-bucket"), \
             patch.object(blob_store, "_get_bucket", return_value=bucket):
            blob_store.write_bytes("data/master.xlsx", b"XLSX DATA")
        _, kwargs = blob.upload_from_string.call_args
        self.assertNotIn("predefined_acl", kwargs)

    def test_uniform_bucket_level_access_falls_back_without_raising(self):
        # A bucket with uniform bucket-level access rejects predefined_acl
        # outright - the upload must still succeed (just without the
        # object-level ACL), not blow up the whole request.
        bucket, blob = _fake_bucket()
        blob.upload_from_string.side_effect = [Exception("individual object ACLs are disabled"), None]
        with patch.object(blob_store, "GCS_BUCKET_NAME", "test-bucket"), \
             patch.object(blob_store, "_get_bucket", return_value=bucket):
            blob_store.write_bytes("brochures/x.pdf", b"PDF DATA", public=True)  # must not raise
        self.assertEqual(blob.upload_from_string.call_count, 2)
        _, retry_kwargs = blob.upload_from_string.call_args
        self.assertNotIn("predefined_acl", retry_kwargs)


class SaveOriginalPdfTests(unittest.TestCase):
    def test_returns_none_in_local_disk_mode(self):
        with patch.object(blob_store, "GCS_BUCKET_NAME", None):
            result = file_store.save_original_pdf(b"PDF DATA", "Business Cube.pdf")
        self.assertIsNone(result)

    def test_returns_a_public_https_url_when_gcs_backed(self):
        bucket, blob = _fake_bucket()
        with patch.object(blob_store, "GCS_BUCKET_NAME", "test-bucket"), \
             patch.object(blob_store, "_get_bucket", return_value=bucket):
            url = file_store.save_original_pdf(b"PDF DATA", "Business Cube.pdf")

        self.assertTrue(url.startswith("https://storage.googleapis.com/test-bucket/brochures/"))
        self.assertIn("Business%20Cube.pdf", url)
        blob.upload_from_string.assert_called_once()
        _, kwargs = blob.upload_from_string.call_args
        self.assertEqual(kwargs.get("predefined_acl"), "publicRead")
        self.assertEqual(kwargs.get("content_type"), "application/pdf")


class IsGenericLinkDomainMatchingTests(unittest.TestCase):
    """The real, confirmed bug found verifying brochure_enrichment.py against
    the real UNION Availability files: is_generic_link used a bare
    netloc.endswith(d) check against KNOWN_NON_BROCHURE_DOMAINS, which is a
    SUBSTRING match, not a same-site-or-subdomain check - "app.box.com"
    (UNION's real brochure host on every one of its real files) ends in the
    same five characters as "x.com" purely by coincidence ("bo-X.COM"),
    so every single real UNION brochure link was silently rejected as if it
    were a Twitter/X profile before this fix."""

    def test_box_com_is_not_mistaken_for_x_com(self):
        self.assertFalse(is_generic_link("https://app.box.com/s/whntw3tqip6cnjeu88d055o3rfbdr019"))

    def test_other_coincidental_x_com_suffix_domains_are_not_mistaken_either(self):
        self.assertFalse(is_generic_link("https://www.netflix.com/watch/12345"))
        self.assertFalse(is_generic_link("https://www.fedex.com/track/12345"))

    def test_real_x_com_profile_is_still_rejected(self):
        self.assertTrue(is_generic_link("https://x.com/somecompany"))

    def test_real_twitter_subdomain_is_still_rejected(self):
        self.assertTrue(is_generic_link("https://mobile.twitter.com/somecompany"))

    def test_bare_box_com_homepage_with_no_path_is_still_generic(self):
        # Not via the domain list at all - via the OTHER is_generic_link rule
        # (empty path, no query) - a real Box share link always has a /s/...
        # path, so this never conflicts with the fix above.
        self.assertTrue(is_generic_link("https://box.com"))

    def test_real_linkedin_company_page_is_still_rejected(self):
        self.assertTrue(is_generic_link("https://www.linkedin.com/company/example"))


if __name__ == "__main__":
    unittest.main()
