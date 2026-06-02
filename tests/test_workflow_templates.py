from __future__ import annotations

import unittest

from aha_cli.domain.models import TASK_WORKFLOW_TEMPLATE_GUIDANCE, TASK_WORKFLOW_TEMPLATES
from aha_cli.domain.workflow_templates import (
    normalize_workflow_template,
    workflow_template_guidance,
    workflow_template_ids,
    workflow_template_metadata,
)


class WorkflowTemplateRegistryTests(unittest.TestCase):
    def test_registry_exposes_ordered_metadata_and_legacy_aliases(self) -> None:
        metadata = workflow_template_metadata()
        ids = [item["id"] for item in metadata]

        self.assertEqual(ids, list(workflow_template_ids()))
        self.assertEqual(ids[0], "auto")
        self.assertIn("fault-debug", ids)
        self.assertIn("embedded-driver", TASK_WORKFLOW_TEMPLATES)
        self.assertEqual(TASK_WORKFLOW_TEMPLATE_GUIDANCE["fault-debug"], workflow_template_guidance("fault-debug"))
        for item in metadata:
            self.assertEqual(set(item), {"id", "label", "description", "guidance", "order"})
            self.assertTrue(item["label"])
            self.assertTrue(item["description"])
            self.assertTrue(item["guidance"])

    def test_registry_normalizes_old_or_unknown_values(self) -> None:
        self.assertEqual(normalize_workflow_template("FAULT-DEBUG"), "fault-debug")
        self.assertEqual(normalize_workflow_template("unknown"), "auto")
        self.assertIn("Fault debug:", workflow_template_guidance("fault-debug"))


if __name__ == "__main__":
    unittest.main()
