# ─────────────────────────────────────────────────────────────
# global_script.py — Railtest example
#
# Flow:
#   1. Run the board script on board 0 (railtest init + tx 1)
#   2. Wait for it to complete
#   3. Send tx 10 on board 0
# ─────────────────────────────────────────────────────────────

def script(scenario):
    board0 = scenario.board(0)  # or scenario.board("tx") or scenario.board("192.168.0.94")

    scenario.print("=== Starting global script ===")

    # Step 1 — Run board script (blocking: waits for completion)
    scenario.print("Running board 0 script...")
    scenario.run_board_scripts(wait=True)
    scenario.print("Board 0 script done.")

    # Step 2 — Send tx 10 on board 0
    scenario.print("Sending tx 10 on board 0...")
    response = board0.cli("tx 10")
    scenario.print(f"tx 10 response: {response.strip()}")

    scenario.print("=== Global script complete ===")
