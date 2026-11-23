"""Immutable local snapshots of comparable strategy runs and their input bars."""

import json
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from backtest.engine import Bar, Position, Result, Settings, Trade, run
from backtest.strategies import STRATEGIES


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, separators=(",", ":"), allow_nan=False), encoding="utf-8")


def max_drawdown(result: Result) -> float:
    peak = result.initial_cash
    drawdown = 0.0
    for _, value in result.equity:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    return drawdown


def save_run_set(
    root: Path,
    bars: list[Bar],
    start: date | None,
    end: date,
    source: str,
    settings: Settings = Settings(),
) -> Path:
    """Run every historical version against identical bars and retain the input snapshot."""
    results = {
        key: run(bars, version.strategy, settings, start) for key, version in STRATEGIES.items()
    }
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    folder = root / f"{now:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    folder.mkdir()
    _write_json(
        folder / "bars.json",
        [
            {**asdict(bar), "day": bar.day.isoformat()}
            for bar in sorted(bars, key=lambda bar: (bar.day, bar.ticker))
        ],
    )
    versions = {}
    for key, version in STRATEGIES.items():
        result = results[key]
        _write_json(
            folder / f"{key}.json",
            {
                "equity": [[day.isoformat(), amount] for day, amount in result.equity],
                "trades": [
                    {
                        **asdict(trade),
                        "entry_day": trade.entry_day.isoformat(),
                        "exit_day": trade.exit_day.isoformat(),
                    }
                    for trade in result.trades
                ],
                "open_positions": {
                    symbol: {**asdict(position), "entry_day": position.entry_day.isoformat()}
                    for symbol, position in result.open_positions.items()
                },
                "cash": result.cash,
                "initial_cash": result.initial_cash,
            },
        )
        versions[key] = {
            "title": version.title,
            "entry": version.entry,
            "exit": version.exit,
            "parameters": asdict(version.strategy),
            "averages": version.averages,
            "final_equity": result.equity[-1][1],
            "total_return": result.total_return,
            "max_drawdown": max_drawdown(result),
            "completed_trades": len(result.trades),
            "open_positions": len(result.open_positions),
            "first_day": result.equity[0][0].isoformat(),
            "last_day": result.equity[-1][0].isoformat(),
        }
    _write_json(
        folder / "manifest.json",
        {
            "captured_at": now.isoformat(),
            "source": source,
            "symbols": sorted({bar.ticker for bar in bars}),
            "requested_start": start.isoformat() if start else None,
            "requested_end": end.isoformat(),
            "settings": asdict(settings),
            "versions": versions,
        },
    )
    return folder


def list_run_sets(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        (
            folder
            for folder in root.iterdir()
            if folder.is_dir() and (folder / "manifest.json").is_file()
        ),
        reverse=True,
    )


def load_manifest(folder: Path) -> dict:
    return json.loads((folder / "manifest.json").read_text(encoding="utf-8"))


def load_result(folder: Path, key: str) -> Result:
    payload = json.loads((folder / f"{key}.json").read_text(encoding="utf-8"))
    return Result(
        [(date.fromisoformat(day), amount) for day, amount in payload["equity"]],
        [
            Trade(
                **{
                    **trade,
                    "entry_day": date.fromisoformat(trade["entry_day"]),
                    "exit_day": date.fromisoformat(trade["exit_day"]),
                }
            )
            for trade in payload["trades"]
        ],
        {
            symbol: Position(**{**position, "entry_day": date.fromisoformat(position["entry_day"])})
            for symbol, position in payload["open_positions"].items()
        },
        payload["cash"],
        payload["initial_cash"],
    )


def load_bars_snapshot(folder: Path) -> list[Bar]:
    payload = json.loads((folder / "bars.json").read_text(encoding="utf-8"))
    return [Bar(**{**bar, "day": date.fromisoformat(bar["day"])}) for bar in payload]
