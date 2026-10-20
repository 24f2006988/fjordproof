import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import terminal_envelope  # noqa: E402
from norway_company_agent.contract import to_contract  # noqa: E402
from norway_company_agent.evidence import evidence  # noqa: E402
from norway_company_agent.footprint_plus import page_contacts  # noqa: E402
from norway_company_agent.identity import assess_social_identity  # noqa: E402
from norway_company_agent.official import normalize_entity  # noqa: E402

PAGE = """<html><body><h1>Kontakt</h1>
<p>Sentralbord: tlf. 22 33 44 55. Daglig leder Kari: <a href="mailto:kari@eksempel.no">kari@eksempel.no</a></p>
<p>Skriv til post@eksempel.no eller ring <a href="tel:+4791234567">912 34 567</a>.</p>
<p>Samarbeidspartner: ola@leverandor.no</p>
<footer>Nettside levert av <a href="mailto:hei@webbyra.no">hei@webbyra.no</a> <a href="tel:+4799999999">999 99 999</a></footer>
</body></html>"""


class ContactsAndNamesTest(unittest.TestCase):
    def test_every_own_contact_is_found_and_agency_credits_are_not(self):
        emails, phones = page_contacts(PAGE, "eksempel.no", "firma@gmail.com")
        self.assertEqual([e for e, _ in emails], ["post@eksempel.no", "kari@eksempel.no"])  # role address first
        self.assertEqual(sorted(p for p, _ in phones), ["+4722334455", "+4791234567"])
        for _, span in emails + phones:
            self.assertTrue(span)

    def test_registry_email_on_another_domain_counts(self):
        emails, _ = page_contacts("<p>E-post: firma@gmail.com</p>", "eksempel.no", "firma@gmail.com")
        self.assertEqual([e for e, _ in emails], ["firma@gmail.com"])

    def test_social_handle_with_the_verified_domain_name_is_accepted(self):
        prof = {"name": "TH KOLBEINSEN INSTALLASJON AS"}
        link = {"platform": "facebook", "url": "https://facebook.com/thkolbeinsen"}
        self.assertTrue(assess_social_identity(prof, link, "www.thkolbeinsen.no")["publishable"])
        self.assertFalse(assess_social_identity(prof, {"platform": "facebook", "url": "https://facebook.com/webbyraet"}, "thkolbeinsen.no")["publishable"])
        self.assertFalse(assess_social_identity({"name": "NORDIC TRADING AS"}, {"platform": "facebook", "url": "https://facebook.com/ntbilsenter"}, "nt.no")["publishable"])

    def test_former_names_and_secondary_industries_are_claimed(self):
        live = normalize_entity({"organisasjonsnummer": "923609016", "navn": "EQUINOR ASA",
                                 "historiskeNavn": [{"fraDato": "2009-11-02 07:56:24", "navn": "STATOIL ASA", "tilDato": "2018-05-16 07:05:45"}],
                                 "naeringskode2": {"kode": "09.101", "beskrivelse": "Boretjenester"}})
        profile = {"organisation_number": "923609016", "name": "EQUINOR ASA", "legal_form": "ASA", "bankrupt": False, "liquidating": False,
                   "evidence": {"registry_live": evidence("registry_live", "available", "official_registry_live",
                                                          "https://data.brreg.no/enhetsregisteret/api/enheter/923609016", value=live)},
                   "run_metrics": {"requests": 1, "latencies_ms": [1]}}
        env = to_contract(terminal_envelope(profile, run_id="t", modules=["registry_live"], started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z"))
        former = [c for c in env["claims"] if c["field"] == "former_name"]
        self.assertEqual(len(former), 1)
        self.assertEqual(former[0]["value"], {"name": "STATOIL ASA", "from": "2009-11-02 07:56:24", "to": "2018-05-16 07:05:45"})
        self.assertEqual(former[0]["source_path"], "enheter[923609016].historiskeNavn[0]")
        self.assertTrue(former[0]["evidence_ids"])
        sec = next(c for c in env["claims"] if c["field"] == "secondary_industry")
        self.assertEqual(sec["value"], {"code": "09.101", "label": "Boretjenester"})
        brief = next(s for s in env["synthesis"]["sections"] if s["label"] == "Company brief")
        self.assertIn("STATOIL ASA (2009-2018)", brief["text"])


if __name__ == "__main__":
    unittest.main()
