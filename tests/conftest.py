import os


# Automated tests must never consume a developer's saved model credentials or quota.
os.environ["CHIP_AGENT_MODEL_ENABLED"] = "false"
os.environ["CHIP_AGENT_RUNTIME_MODE"] = "test"

