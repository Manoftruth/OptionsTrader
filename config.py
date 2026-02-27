"""
OptionsAgent Configuration — MAXIMUM AGGRESSION BUILD
"""
CONFIG = {
    "quiver_token":             "YOUR_QUIVER_TOKEN_HERE",
    "tradier_token":            "YOUR_TRADIER_TOKEN_HERE",
    "account_id":               "YOUR_ACCOUNT_ID_HERE",
    "sandbox":                  True,  # Set False only for live trading
    "capital_limit":            500.00,
    "max_trade_size":           125.00,
    "min_capital_to_trade":     20.00,
    "daily_loss_limit":         75.00,
    "max_contract_price":       3.00,
    "max_concurrent_positions": 2,
    "take_profit_pct":          38.0,
    "stop_loss_pct":            38.0,
    "min_signal_score":         13,
    "min_days_to_expiry":       0,
    "scan_interval_seconds":    200,
    "watchlist": [
        "TSLA", "NVDA", "COIN", "MSTR", "AMD",
        "PLTR", "HOOD", "RBLX",
        "TQQQ", "SOXL", "SPXL", "LABU",
        "SPY", "QQQ",
    ]
}
