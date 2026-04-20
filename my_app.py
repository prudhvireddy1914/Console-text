from console_text import console

def connect_to_database(host: str):
    """Simulate a DB connection that might fail."""
    if host == "bad-host":
        raise ConnectionError(f"Cannot reach database at {host}")
    console.log(f"Connected to database at {host}")   # local only, no Telegram


def process_payment(user_id: int, amount: float):
    """Simulate payment processing."""
    if amount <= 0:
        # 🔴 Sends Telegram alert + prints locally
        console.text(
            f"Invalid payment amount {amount} for user {user_id}",
            level="ERROR",
            extra={"user_id": user_id, "amount": amount},
        )
        return False

    if amount > 10_000:
        # ⚠️  Warning-level alert
        console.warning(
            f"Large transaction detected: ₹{amount} by user {user_id}",
            extra={"user_id": user_id},
        )

    console.log(f"Payment of ₹{amount} processed for user {user_id}")
    return True


def main():
    # 1. Normal log — prints to terminal, NO Telegram alert
    console.log("Server starting up...")

    # 2. DB connection failure — sends Telegram alert
    try:
        connect_to_database("bad-host")
    except ConnectionError as e:
        console.text(str(e), level="CRITICAL", include_traceback=True)

    # 3. Successful connection
    connect_to_database("db.prod.internal")

    # 4. Payment edge cases
    process_payment(user_id=42, amount=-50)        # triggers ERROR alert
    process_payment(user_id=7, amount=15_000)      # triggers WARNING alert
    process_payment(user_id=1, amount=299)         # normal, no alert

    # 5. Anywhere you want a quick alert — one liner:
    console.text("Scheduled job failed: nightly_report", level="ERROR")

    # 6. Print the dashboard summary
    console.dashboard()


if __name__ == "__main__":
    main()
