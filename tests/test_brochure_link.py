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

from brochure_link_resolver import (
    finalize_brochure_link, is_canva_view_link, is_floorplan_not_brochure_url, is_generic_link, is_gpe_flipbook_link,
    is_kitt_brochure_preview_link, is_pitch_view_link, looks_like_url,
)
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


class LooksLikeUrlTests(unittest.TestCase):
    """looks_like_url - distinguishes a genuine link (with or without an
    explicit scheme) from a placeholder a provider uses to mean "no
    brochure yet"."""

    def test_explicit_scheme_is_always_accepted(self):
        self.assertTrue(looks_like_url("https://app.box.com/s/abc123"))
        self.assertTrue(looks_like_url("http://example.com"))

    def test_scheme_less_domain_with_path_is_accepted(self):
        self.assertTrue(looks_like_url("app.box.com/s/abc123"))

    def test_scheme_less_domain_with_a_port_is_accepted(self):
        # Confirmed real gap: a genuine hyperlink target recovered from a
        # staging file this app itself already wrote, that happens to
        # include an explicit port, used to fail this check and be
        # silently nulled on reload (see storage.file_store._sanitize_url_
        # like_fields) even though nothing about the link had changed.
        self.assertTrue(looks_like_url("app.box.com:8443/s/abc123"))

    def test_scheme_less_domain_with_a_bare_query_string_is_accepted(self):
        self.assertTrue(looks_like_url("example.com?ref=1"))

    def test_bare_domain_with_no_path_is_accepted(self):
        self.assertTrue(looks_like_url("workplaceplus.co.uk"))

    def test_tbc_placeholder_is_rejected(self):
        self.assertFalse(looks_like_url("TBC"))

    def test_coming_soon_placeholder_is_rejected(self):
        self.assertFalse(looks_like_url("Coming Soon"))

    def test_n_a_placeholder_is_rejected(self):
        self.assertFalse(looks_like_url("N/A"))

    def test_none_placeholder_is_rejected(self):
        self.assertFalse(looks_like_url("None"))

    def test_dash_placeholder_is_rejected(self):
        self.assertFalse(looks_like_url("-"))

    def test_blank_is_rejected(self):
        self.assertFalse(looks_like_url(""))
        self.assertFalse(looks_like_url(None))
        self.assertFalse(looks_like_url("   "))


class IsFloorplanNotBrochureUrlTests(unittest.TestCase):
    def test_pure_floorplan_url_is_flagged(self):
        self.assertTrue(is_floorplan_not_brochure_url("https://example.com/floorplans/a.pdf"))

    def test_pure_brochure_url_is_not_flagged(self):
        self.assertFalse(is_floorplan_not_brochure_url("https://example.com/brochure.pdf"))

    def test_combined_brochure_and_floorplan_filename_is_not_flagged(self):
        # Real, common combined-document naming pattern - a genuine
        # brochure link must never be discarded just because its own name
        # also happens to mention floor plans.
        self.assertFalse(
            is_floorplan_not_brochure_url("https://example.com/Building-Brochure-and-Floorplans.pdf"),
        )

    def test_blank_is_not_flagged(self):
        self.assertFalse(is_floorplan_not_brochure_url(None))
        self.assertFalse(is_floorplan_not_brochure_url(""))


class IsCanvaViewLinkTests(unittest.TestCase):
    def test_the_real_public_example_link_is_recognised(self):
        self.assertTrue(is_canva_view_link(
            "https://www.canva.com/design/DAGzsWW-Yp8/s8tPVTQe6HUQa939xX0XQw/view"
            "?utm_content=DAGzsWW-Yp8&utm_campaign=designshare#7"
        ))

    def test_a_view_link_with_no_query_or_fragment_is_recognised(self):
        self.assertTrue(is_canva_view_link("https://canva.com/design/abc123/def456/view"))

    def test_a_bare_canva_homepage_is_not_a_view_link(self):
        self.assertFalse(is_canva_view_link("https://www.canva.com/"))

    def test_a_canva_template_gallery_page_is_not_a_view_link(self):
        self.assertFalse(is_canva_view_link("https://www.canva.com/templates/"))

    def test_a_canva_edit_link_is_not_a_view_link(self):
        # Requires an owner's own editor session - never a public link this
        # pipeline could read regardless, but deliberately a DIFFERENT shape
        # from /view, so this stays a narrow, structural match only.
        self.assertFalse(is_canva_view_link("https://www.canva.com/design/DAGzsWW-Yp8/edit"))

    def test_an_unrelated_domain_is_never_matched(self):
        self.assertFalse(is_canva_view_link("https://example.com/design/abc/def/view"))

    def test_blank_is_not_a_view_link(self):
        self.assertFalse(is_canva_view_link(None))
        self.assertFalse(is_canva_view_link(""))

    def test_a_canva_short_link_is_recognised(self):
        # Real, confirmed shape (dozens of real links in this project's own
        # Workplace Company fixture) - every one redirects to a real
        # canva.com/design/{id}/{token}/view URL. Previously NOT
        # recognized at all, so it silently fell through to the ordinary
        # generic fetch path instead of being routed to the Canva renderer.
        self.assertTrue(is_canva_view_link("https://canva.link/45k34aansogxr2a"))

    def test_a_canva_short_link_with_a_query_string_is_still_recognised(self):
        self.assertTrue(is_canva_view_link("https://canva.link/45k34aansogxr2a?utm_source=x"))

    def test_an_unrelated_dot_link_domain_is_not_matched(self):
        self.assertFalse(is_canva_view_link("https://example.link/45k34aansogxr2a"))


class IsPitchViewLinkTests(unittest.TestCase):
    def test_real_gpe_style_view_links_are_recognised(self):
        self.assertTrue(is_pitch_view_link("https://pitch.com/v/1-finsbury-brochure-4jnj9d"))
        self.assertTrue(is_pitch_view_link("https://pitch.com/v/hallmark-6th-floor-jdfuuc"))

    def test_a_bare_pitch_homepage_is_not_a_view_link(self):
        self.assertFalse(is_pitch_view_link("https://pitch.com/"))

    def test_an_unrelated_domain_is_never_matched(self):
        self.assertFalse(is_pitch_view_link("https://example.com/v/abc"))

    def test_a_canva_link_is_not_a_pitch_link(self):
        self.assertFalse(is_pitch_view_link("https://www.canva.com/design/abc/def/view"))

    def test_blank_is_not_a_view_link(self):
        self.assertFalse(is_pitch_view_link(None))
        self.assertFalse(is_pitch_view_link(""))


class IsGpeFlipbookLinkTests(unittest.TestCase):
    """GPE's own branded "fm.gpe.co.uk" custom domain for Pitch's Managed
    Links feature - confirmed real shapes in tests/sample_docs/GPE.eml."""

    def test_real_gpe_flipbook_link_is_recognised(self):
        self.assertTrue(is_gpe_flipbook_link("https://fm.gpe.co.uk/v/gpe-nineteen-wells-street-6hqnfd"))

    def test_the_trailing_uuid_segment_variant_is_also_recognised(self):
        # Real second shape from the same GPE.eml fixture - a Dynamics
        # marketing-tracked link carries an extra recipient-specific UUID
        # path segment after the slug.
        self.assertTrue(is_gpe_flipbook_link(
            "https://fm.gpe.co.uk/v/gpe-availability-schedule-zu7yk2/b812cdcc-7bbb-429b-8af5-d000b8032853"
        ))

    def test_a_bare_fm_gpe_co_uk_homepage_is_not_a_flipbook_link(self):
        self.assertFalse(is_gpe_flipbook_link("https://fm.gpe.co.uk/"))

    def test_a_plain_gpe_co_uk_link_is_not_a_flipbook_link(self):
        # "gpe.co.uk/portfolio/..." - GPE's own ordinary corporate site,
        # a completely different host from the "fm." flipbook subdomain.
        self.assertFalse(is_gpe_flipbook_link("https://gpe.co.uk/portfolio/city-tower"))

    def test_a_plain_pitch_com_link_is_not_a_gpe_flipbook_link(self):
        # Same underlying player, but this detector is matched purely on
        # the URL's own host - a genuine pitch.com/v/... link stays
        # is_pitch_view_link's own job, never double-counted here too.
        self.assertFalse(is_gpe_flipbook_link("https://pitch.com/v/1-finsbury-brochure-4jnj9d"))

    def test_blank_is_not_a_flipbook_link(self):
        self.assertFalse(is_gpe_flipbook_link(None))
        self.assertFalse(is_gpe_flipbook_link(""))


class IsKittBrochurePreviewLinkTests(unittest.TestCase):
    """Kitt's own "brochures.kittoffices.com/brochures/preview" brochure-
    preview app - confirmed real shapes via live Playwright recon against
    four real production links from a real Kitt's upload."""

    def test_real_preview_link_is_recognised(self):
        self.assertTrue(is_kitt_brochure_preview_link(
            "https://brochures.kittoffices.com/brochures/preview?entity%5B9e40cdea-02a1-44a5-9599-"
            "c3ed1567c117%5D=unit&display_label=Open+brochure"
        ))

    def test_variant_with_empty_template_param_is_also_recognised(self):
        # Real second shape from the same confirmed batch - some real
        # links carry an empty "template=" query param, some don't.
        self.assertTrue(is_kitt_brochure_preview_link(
            "https://brochures.kittoffices.com/brochures/preview?entity%5B7c50678c-7d16-4a8b-8f85-"
            "16dcd98b9a99%5D=unit&template=&display_label=Open+brochure"
        ))

    def test_a_bare_preview_path_with_no_entity_query_is_still_recognised(self):
        # The path shape alone is what's matched - the query string isn't
        # parsed precisely (too fragile across encoding variations, same
        # "loose" precedent as is_gpe_flipbook_link's own trailing-segment
        # handling).
        self.assertTrue(is_kitt_brochure_preview_link("https://brochures.kittoffices.com/brochures/preview"))

    def test_kitts_ordinary_marketing_site_is_not_a_preview_link(self):
        self.assertFalse(is_kitt_brochure_preview_link("https://www.kittoffices.com/"))

    def test_a_different_path_on_the_same_host_is_not_a_preview_link(self):
        self.assertFalse(is_kitt_brochure_preview_link("https://brochures.kittoffices.com/some-other-page"))

    def test_a_plain_pitch_com_link_is_not_a_kitt_preview_link(self):
        self.assertFalse(is_kitt_brochure_preview_link("https://pitch.com/v/1-finsbury-brochure-4jnj9d"))

    def test_blank_is_not_a_preview_link(self):
        self.assertFalse(is_kitt_brochure_preview_link(None))
        self.assertFalse(is_kitt_brochure_preview_link(""))


class FinalizeBrochureLinkFloorplanGuardTests(unittest.TestCase):
    def test_unambiguous_floorplan_link_is_discarded(self):
        result = finalize_brochure_link(
            "https://example.com/floorplans/a.pdf", is_pdf=True,
            pdf_fallback_link="https://storage.googleapis.com/bucket/x.pdf",
        )
        self.assertEqual(result, "https://storage.googleapis.com/bucket/x.pdf")

    def test_combined_brochure_and_floorplan_link_is_kept(self):
        result = finalize_brochure_link(
            "https://example.com/Building-Brochure-and-Floorplans.pdf", is_pdf=True,
            pdf_fallback_link="https://storage.googleapis.com/bucket/x.pdf",
        )
        self.assertEqual(result, "https://example.com/Building-Brochure-and-Floorplans.pdf")


if __name__ == "__main__":
    unittest.main()
