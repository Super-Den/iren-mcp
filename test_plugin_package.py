import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class PluginPackageTests(unittest.TestCase):
    def read_json(self, relative):
        return json.loads((ROOT / relative).read_text(encoding="utf-8"))

    def test_portable_manifest_and_components(self):
        manifest = self.read_json("plugin.json")
        self.assertEqual(manifest["name"], "iren-mcp")
        self.assertEqual(manifest["version"], "0.1.4")
        self.assertTrue((ROOT / "mcp.json").is_file())
        self.assertTrue((ROOT / "assets/iren-mcp.svg").is_file())
        onboarding = manifest["extensions"]["com.openai"]["onboardingSkill"]
        self.assertTrue((ROOT / onboarding[2:]).is_file())
        mcp = self.read_json(".mcp.json")
        server = mcp["mcpServers"]["iren"]
        self.assertEqual(server["command"], "py")
        self.assertEqual(server["cwd"], "${PLUGIN_ROOT}")

    def test_marketplace_points_inside_repo(self):
        marketplace = self.read_json(".agents/plugins/marketplace.json")
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["source"]["path"], "./")
        self.assertEqual(entry["policy"]["installation"], "AVAILABLE")
        self.assertEqual(entry["category"], "Developer Tools")


if __name__ == "__main__":
    unittest.main()
