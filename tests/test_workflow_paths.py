"""Check file paths using real workflow JavaScript without running n8n."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_ai_instruction_workflow import ROOT, WORKFLOW, sample_input

RUNNER = """
const fs = require('fs');
const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
const env = payload.env;
const paths = [];
for (const node of payload.workflow.nodes) {
  const params = node.parameters;
  if (params.fileSelector) {
    const expression = params.fileSelector;
    const path = new Function('$env', 'return (' + expression.slice(3, -2) + ')')(env);
    paths.push({kind: 'config', path});
  }
  if (['Build Markdown Files', 'Build Structured Records'].includes(node.name)) {
    const items = new Function('$json', '$env', params.jsCode)(payload.input, env);
    for (const item of items) {
      paths.push({kind: 'output', path: item.json.filePath, relative: item.json.relativePath});
    }
  }
}
process.stdout.write(JSON.stringify(paths));
"""


class WorkflowPathTests(unittest.TestCase):
    def run_paths(self, env):
        node = os.environ.get("AUTOARTICLE_NODE") or shutil.which("node")
        if not node:
            self.skipTest("Node.js required")
        return subprocess.run(
            [node, "-e", RUNNER],
            input=json.dumps({"workflow": json.loads(WORKFLOW.read_text()),
                              "input": sample_input(), "env": env}),
            text=True, capture_output=True, timeout=30,
        )

    def test_all_reads_and_writes_follow_runtime_root(self):
        for root in ("/project", "/srv/another checkout/news/", "/tmp/article-app///", "/"):
            with self.subTest(root=root):
                result = self.run_paths({"PROJECT_ROOT": root})
                self.assertEqual(result.returncode, 0, result.stderr)
                paths = json.loads(result.stdout)
                self.assertEqual(sum(p["kind"] == "config" for p in paths), 3)
                self.assertEqual(sum(p["kind"] == "output" for p in paths), 8)
                for entry in paths:
                    suffix = "config/keywords.json" if entry["kind"] == "config" else entry["relative"]
                    self.assertEqual(entry["path"], root.rstrip("/") + "/" + suffix)

    def test_missing_or_relative_root_fails_each_path_producer(self):
        workflow = json.loads(WORKFLOW.read_text())
        producers = [n for n in workflow["nodes"] if n["parameters"].get("fileSelector")
                     or n["name"] in ("Build Markdown Files", "Build Structured Records")]
        node = os.environ.get("AUTOARTICLE_NODE") or shutil.which("node")
        if not node:
            self.skipTest("Node.js required")
        for producer in producers:
            for env in ({}, {"PROJECT_ROOT": ""}, {"PROJECT_ROOT": "./content"}):
                with self.subTest(node=producer["name"], env=env):
                    result = subprocess.run(
                        [node, "-e", RUNNER],
                        input=json.dumps({"workflow": {"nodes": [producer]},
                                          "input": sample_input(), "env": env}),
                        text=True, capture_output=True, timeout=30,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("PROJECT_ROOT must be an absolute project path", result.stderr)

    @unittest.skipUnless(os.name == "posix", "Bash startup runs on WSL/Linux")
    def test_startup_detects_moved_checkout_from_unrelated_cwd(self):
        with tempfile.TemporaryDirectory() as temp:
            checkout = Path(temp) / "moved checkout"
            scripts = checkout / "scripts"
            scripts.mkdir(parents=True)
            shutil.copyfile(ROOT / "scripts/start_n8n_with_file_access.sh",
                            scripts / "start_n8n_with_file_access.sh")
            fake = Path(temp) / "fake-n8n"
            fake.write_text("#!/bin/sh\nprintf 'ROOT=%s\\nACCESS=%s\\nBLOCK=%s\\n' \"$PROJECT_ROOT\" \"$N8N_RESTRICT_FILE_ACCESS_TO\" \"$N8N_BLOCK_ENV_ACCESS_IN_NODE\"\n")
            fake.chmod(0o755)
            env = dict(os.environ, N8N_BIN=str(fake), PROJECT_ROOT="/old/location")
            env.pop("N8N_RESTRICT_FILE_ACCESS_TO", None)
            env.pop("N8N_BLOCK_ENV_ACCESS_IN_NODE", None)
            result = subprocess.run(
                ["bash", str(scripts / "start_n8n_with_file_access.sh")],
                cwd="/", env=env, text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ROOT=" + str(checkout), result.stdout)
            self.assertIn("ACCESS=" + str(checkout), result.stdout)
            self.assertIn("BLOCK=false", result.stdout)
