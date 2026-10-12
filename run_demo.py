"""Warden demo entry point.

    python run_demo.py greenfield                         # offline replay, you approve each gate
    python run_demo.py greenfield --auto-approve          # offline replay, gates auto-approved
    python run_demo.py greenfield --record --auto-approve # call Gemini for unrecorded prompts
"""

from demo.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
