"""WATCHLIST + непрерывный скан публичных Telegram-каналов КӨЗ.

store   — персистентный JSON-список каналов (data/watchlist.json).
service — scan_watchlist(conn): прогон всех каналов через ingestion.scan_telegram.
routes  — REST поверх (авто-подключается main.py).
"""
