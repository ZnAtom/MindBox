import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import httpx

import workflow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "reference.png"
        self.image.write_bytes(b"test image fixture")
        self.manifest = self.root / "prepared.json"
        self.data = {"id": "test", "seed": 42, "prepared_images": [{"file": str(self.image), "label": "F"}]}
        workflow.write_json(self.manifest, self.data)

    @patch("workflow.Client")
    def test_checkpoint_prevents_duplicate_billing_and_changed_request(self, client):
        client.return_value.call.return_value = {"uuid": "task-1", "jobs": {"subscription_key": "task-only-token"}, "consumed": 0.5}
        task = workflow.submit(self.manifest, self.root)
        self.assertEqual(workflow.submit(self.manifest, self.root), task)
        self.assertEqual(client.return_value.call.call_count, 1)
        self.data["seed"] = 43
        workflow.write_json(self.manifest, self.data)
        with self.assertRaises(ValueError):
            workflow.submit(self.manifest, self.root)
        self.assertEqual(client.return_value.call.call_count, 1)

    @patch("workflow.Client")
    def test_uncertain_submission_is_not_retried(self, client):
        client.return_value.call.side_effect = workflow.ApiError("timeout", uncertain=True)
        with self.assertRaises(workflow.ApiError):
            workflow.submit(self.manifest, self.root)
        checkpoint = self.root / "tasks/test/task.json"
        self.assertEqual(workflow.read_json(checkpoint)["state"], "unconfirmed")
        with self.assertRaises(ValueError):
            workflow.submit(self.manifest, self.root)
        self.assertEqual(client.return_value.call.call_count, 1)

    def test_application_error_is_not_successful_http(self):
        client = workflow.Client.__new__(workflow.Client)
        client.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(201, json={"error": "ASSET_NOT_AVAILABLE", "uuid": "invalid"})))
        self.addCleanup(client.client.close)
        with self.assertRaisesRegex(workflow.ApiError, "ASSET_NOT_AVAILABLE"):
            client.call("POST", "bang")

    @patch("workflow.Client")
    def test_incomplete_jobs_cannot_download(self, client):
        task = self.root / "task.json"
        workflow.write_json(task, {"uuid": "task-1", "jobs": {"subscription_key": "task-token"}})
        client.return_value.call.return_value = {"jobs": [{"status": "Done"}, {"status": "Running"}]}
        with self.assertRaisesRegex(workflow.ApiError, "finish"):
            workflow.download(task)
        self.assertEqual(client.return_value.call.call_count, 1)

    @patch("workflow.Client")
    def test_uploaded_split_includes_model_and_reference_without_asset_id(self, client):
        client.return_value.call.return_value = {"uuid": "split-1", "jobs": {"subscription_key": "task-token"}}
        model = self.root / "model.glb"
        model.write_bytes(b"model fixture")
        workflow.submit(self.manifest, self.root, split_model=model)
        call = client.return_value.call.call_args
        self.assertEqual(call.args, ("POST", "bang"))
        self.assertNotIn("asset_id", call.kwargs["data"])
        self.assertEqual([entry[0] for entry in call.kwargs["files"]], ["model", "image"])
        self.assertEqual(call.kwargs["data"]["material"], "None")
        self.assertEqual(call.kwargs["data"]["quality_override"], "10000")

    def test_input_limits_are_checked_before_submission(self):
        self.data["prepared_images"] *= 6
        with self.assertRaisesRegex(ValueError, "five"):
            workflow.generation_fields(self.data)

    def test_neutral_style_preserves_binary_geometry(self):
        document = {"asset": {"version": "2.0"}, "accessors": [{"count": 3}],
                    "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "COLOR_0": 1}, "material": 2}]}],
                    "nodes": [{"mesh": 0}], "textures": [{}], "images": [{}]}
        encoded = json.dumps(document).encode()
        encoded += b" " * (-len(encoded) % 4)
        binary = b"12345678"
        chunks = struct.pack("<II", len(encoded), 0x4E4F534A) + encoded + struct.pack("<II", len(binary), 0x004E4942) + binary
        source, target = self.root / "raw.glb", self.root / "neutral.glb"
        source.write_bytes(struct.pack("<III", 0x46546C67, 2, 12 + len(chunks)) + chunks)
        workflow.style_glb(source, target)
        styled, result_binary = workflow.read_glb(target)
        self.assertEqual(result_binary, binary)
        self.assertEqual(styled["nodes"], document["nodes"])
        self.assertNotIn("textures", styled)
        self.assertEqual(len(styled["materials"]), 1)
        self.assertNotIn("COLOR_0", styled["meshes"][0]["primitives"][0]["attributes"])
        self.assertEqual(workflow.read_json(target.with_suffix(".stats.json"))["mesh_faces"], 1)


if __name__ == "__main__":
    unittest.main()
