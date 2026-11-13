import unittest

from norway_company_agent.official import normalize_group

TREE = {"navn": "TOPP AS", "organisasjonsnummer": "111111111", "organisasjonsform": {"kode": "AS"}, "children": [
    {"navn": "DATTER AS", "organisasjonsnummer": "222222222", "parentOrganisasjonsnummer": "111111111", "parentNavn": "TOPP AS",
     "grunnlag": "100 %", "dato": "2020-01-01", "knytningsform": {"kode": "KDAT"}, "children": [
         {"navn": "BARNEBARN AS", "organisasjonsnummer": "444444444", "parentOrganisasjonsnummer": "222222222", "parentNavn": "DATTER AS"}]},
    {"navn": "SØSTER AS", "organisasjonsnummer": "333333333", "parentOrganisasjonsnummer": "111111111", "parentNavn": "TOPP AS"}]}


class GroupSubject(unittest.TestCase):
    def test_subsidiary_value_is_about_the_subsidiary_and_drops_sisters(self):
        v = normalize_group(TREE, "222222222")
        self.assertEqual((v["organisasjonsnummer"], v["navn"], v["position"]), ("222222222", "DATTER AS", "subsidiary"))
        self.assertEqual(v["parent"]["organisasjonsnummer"], "111111111")
        self.assertEqual(v["parent"]["grunnlag"], "100 %")
        self.assertEqual([c["organisasjonsnummer"] for c in v["children"]], ["444444444"])
        self.assertNotIn("333333333", str(v))

    def test_group_parent_keeps_every_subsidiary(self):
        v = normalize_group(TREE, "111111111")
        self.assertEqual(v["position"], "group_parent")
        self.assertNotIn("parent", v)
        self.assertEqual({c["organisasjonsnummer"] for c in v["children"]}, {"222222222", "333333333", "444444444"})

    def test_company_missing_from_tree_never_takes_the_root_identity(self):
        v = normalize_group(TREE, "999999999")
        self.assertEqual((v["organisasjonsnummer"], v["position"], v["children"]), ("999999999", "member", []))


if __name__ == "__main__":
    unittest.main()
