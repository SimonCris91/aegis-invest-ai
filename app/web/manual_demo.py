"""User-directed Demo orders. No strategy, agent, or automatic sizing."""

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from uuid import uuid4

from app.brokers.etoro.client import EtoroApiError, EtoroReadClient
from app.brokers.etoro.demo import DEMO_ORDER_URL
from app.brokers.etoro.http import DisciplinedHttpClient, UrllibTransport
from app.brokers.etoro.runtime import runtime_credentials, runtime_settings
from app.config.loader import load_config, load_runtime_values
from app.domain.enums import Currency, EtoroTransportMode
from app.domain.market import MarketQuote


class ManualReadClient(EtoroReadClient):
    """Manual ticket uses the documented v2 realtime bid/ask feed."""

    def quote_with_diagnostics(self, instrument_id, symbol):
        response = self._get_response(
            f"/api/v2/market-data/rates?instrumentIds={instrument_id}",
            extra_headers={"Cache-Control": "no-cache"},
        )
        rows = response.json().get("results", [])
        matches = [r for r in rows if str(r.get("instrumentId")) == str(instrument_id)]
        if len(matches) != 1 or matches[0].get("quoteType") != "realtime":
            raise ManualOrderError("Quotazione in tempo reale non disponibile per lo strumento.")
        row = matches[0]
        as_of = datetime.fromisoformat(str(row["date"]).replace("Z", "+00:00"))
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=UTC)  # v2 documents UTC, including unzoned ISO values.
        bid, ask = number(row.get("bid")), number(row.get("ask"))
        if not 0 < bid <= ask:
            raise ManualOrderError("Quotazione bid/ask non valida.")
        quote = MarketQuote(
            instrument_id=instrument_id,
            symbol=symbol,
            price=bid,
            bid=bid,
            ask=ask,
            as_of=as_of,
            currency=Currency.USD,
            source="etoro-v2-realtime",
        )
        date = next((v for k, v in response.headers.items() if k.lower() == "date"), None)
        return quote, (row["date"],), parsedate_to_datetime(date) if date else None


class ManualOrderError(ValueError):
    pass


def number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ManualOrderError("Valore numerico non valido.") from None
    if not result.is_finite():
        raise ManualOrderError("Valore numerico non valido.")
    return result


def request_order(data):
    if not isinstance(data, dict) or set(data) - {
        "mode",
        "instrument_id",
        "symbol",
        "side",
        "amount",
        "position_id",
        "close_partial",
        "units",
    }:
        raise ManualOrderError("Ticket non valido.")
    if data.get("mode") != "MANUAL" or data.get("side") not in {"BUY", "SELL"}:
        raise ManualOrderError("Seleziona un ordine manuale BUY o SELL.")
    iid = str(data.get("instrument_id", ""))
    symbol = data.get("symbol", "")
    if (
        not re.fullmatch(r"[1-9][0-9]{0,12}", iid)
        or not isinstance(symbol, str)
        or not 1 <= len(symbol) <= 64
    ):
        raise ManualOrderError("Seleziona uno strumento valido.")
    amount = number(data.get("amount"))
    if amount <= 0 or amount > Decimal("100000000") or amount != amount.quantize(Decimal("0.01")):
        raise ManualOrderError("Inserisci un importo positivo in USD, con massimo due decimali.")
    position = str(data.get("position_id") or "")
    if data["side"] == "SELL" and not re.fullmatch(r"[1-9][0-9]{0,18}", position):
        raise ManualOrderError("Per vendere seleziona una posizione Demo posseduta.")
    return {
        "instrument_id": int(iid),
        "symbol": symbol,
        "side": data["side"],
        "amount": str(amount),
        "position_id": position,
        "close_partial": bool(data.get("close_partial", False)),
        "units": str(data.get("units")) if data.get("units") not in (None, "") else None,
    }


class ManualDemoOrders:
    def __init__(self, path=None, *, factory=None, clock=None):
        self.path = path or Path(__file__).resolve().parents[2] / "work/manual-demo-orders.sqlite3"
        self.factory = factory or self._live_clients
        self.clock = clock or (lambda: datetime.now(UTC))

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=60)
        db.execute(
            "CREATE TABLE IF NOT EXISTS manual_orders (id TEXT PRIMARY KEY, "
            "ticket TEXT NOT NULL, created REAL NOT NULL, state TEXT NOT NULL, result TEXT)"
        )
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _live_clients():
        values = load_runtime_values()
        config = load_config(values)
        if (
            config.operating_mode.value != "ETORO_DEMO"
            or not config.etoro_api_enabled
            or not config.etoro_demo_execution_enabled
            or config.production_trading_enabled
        ):
            raise ManualOrderError("Invio Demo non abilitato nella configurazione.")
        credentials = runtime_credentials(values)
        if credentials is None:
            raise ManualOrderError("Credenziali Demo non disponibili.")
        http = DisciplinedHttpClient(
            UrllibTransport(EtoroTransportMode.DIRECT), max_read_attempts=1
        )
        return ManualReadClient(credentials, http), http, credentials, runtime_settings(values)

    def _check(self, ticket, *, prepared=None):
        client, http, credentials, settings = self.factory()
        try:
            identity = client.identity()
        except EtoroApiError as exc:
            if exc.status == 401:
                raise ManualOrderError(
                    "Credenziali eToro non accettate (HTTP 401). Verifica che ETORO_API_KEY "
                    "e ETORO_USER_KEY siano della stessa chiave Demo Write."
                ) from None
            raise ManualOrderError("Verifica identità eToro non riuscita.") from None
        if "etoro-public:trade.demo:write" not in identity.scopes:
            raise ManualOrderError(
                "La chiave eToro non autorizza ordini Demo. In eToro: Impostazioni > Trading > "
                "API Key Management, crea una chiave con Environment Demo e Permissions Write; "
                "aggiorna ETORO_USER_KEY nel file .env. Non inserire la chiave nella chat."
            )
        if not (settings.expected_username or settings.expected_gcid):
            raise ManualOrderError("Identità del conto atteso non configurata.")
        if (settings.expected_gcid and identity.stable_user_id != settings.expected_gcid) or (
            settings.expected_username
            and (identity.username or "").casefold() != settings.expected_username.casefold()
        ):
            raise ManualOrderError("Il conto autenticato non corrisponde al conto configurato.")
        account = f"{identity.stable_user_id}:{identity.demo_account_id}"
        if prepared is not None and prepared.get("account") != account:
            raise ManualOrderError("Il conto è cambiato dopo la preparazione dell'ordine.")
        iid, symbol = ticket["instrument_id"], ticket["symbol"]
        resolution = client.resolve_instrument_id(iid, symbol=symbol)
        if (
            not resolution.resolved
            or resolution.instrument_id != iid
            or resolution.internal_symbol_full.casefold() != symbol.casefold()
            or resolution.is_currently_tradable is not True
            or resolution.is_active_in_platform is not True
            or resolution.is_delisted is not False
            or resolution.is_hidden_from_client is not False
        ):
            raise ManualOrderError("Lo strumento non è attualmente negoziabile su eToro.")
        eligibility = client.demo_eligibility(iid, symbol, currency=Currency.USD)
        if not eligibility.verified or eligibility.currency != Currency.USD:
            raise ManualOrderError("Idoneità Demo in USD non verificata.")
        quote, _, server_now = client.quote_with_diagnostics(iid, symbol)
        reference = server_now or self.clock()
        age = (reference - quote.as_of).total_seconds()
        if (
            not -settings.maximum_future_quote_skew_seconds
            <= age
            <= settings.maximum_quote_age_seconds
        ):
            raise ManualOrderError("Prezzo eToro non aggiornato: riprepara l'ordine tra poco.")
        portfolio = client.demo_portfolio_payload()
        data = portfolio.get("clientPortfolio") if isinstance(portfolio, dict) else None
        if not isinstance(data, dict):
            raise ManualOrderError("Portafoglio Demo non disponibile.")
        if ticket["side"] == "BUY":
            if resolution.is_buy_enabled is not True or not eligibility.allow_open:
                raise ManualOrderError("Acquisto non consentito da eToro.")
            # A manual BUY is exactly one unit at the current live Ask. The
            # browser value is display-only and is never trusted for execution.
            price = number(quote.ask if quote.ask is not None else quote.price)
            amount = price
            amount = amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if amount <= 0:
                raise ManualOrderError("Importo live non valido.")
            units = (amount / number(price)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
            ticket = {**ticket, "amount": str(amount), "units": str(units)}
            if amount < eligibility.minimum_position:
                raise ManualOrderError(f"Importo minimo eToro: {eligibility.minimum_position} USD.")
            cash = number(data.get("credit"))
            for key in ("ordersForOpen", "orders"):
                rows = data.get(key)
                if not isinstance(rows, list):
                    raise ManualOrderError(
                        "Ordini pendenti non disponibili per verificare il saldo."
                    )
                for row in rows:
                    if key == "orders" or row.get("mirrorID") == 0:
                        cash -= number(row.get("amount"))
            if amount > cash:
                raise ManualOrderError(f"Saldo Demo disponibile insufficiente: {cash} USD.")
            if (
                eligibility.max_units_per_order is not None
                and amount / price > eligibility.max_units_per_order
            ):
                raise ManualOrderError("Quantità superiore al massimo consentito da eToro.")
            payload = {
                "action": "open",
                "transaction": "buy",
                "instrumentId": iid,
                "settlementType": "real",
                "orderType": "mkt",
                "leverage": 1,
                "amount": float(amount),
                "orderCurrency": "usd",
                "stopLossType": "fixed",
            }
            endpoint = DEMO_ORDER_URL
        else:
            if eligibility.allow_close is not True:
                raise ManualOrderError("Chiusura non consentita da eToro.")
            rows = data.get("positions", [])
            matches = [
                p
                for p in rows
                if str(p.get("positionID")) == ticket["position_id"]
                and str(p.get("instrumentID")) == str(iid)
                and p.get("isBuy") is True
            ]
            if len(matches) != 1 or matches[0].get("mirrorID", 0) != 0:
                raise ManualOrderError("Posizione Demo manuale non trovata.")
            price = number(quote.bid if quote.bid is not None else quote.price)
            position_units = number(matches[0].get("units"))
            requested_units = number(ticket["units"]) if ticket.get("units") else position_units
            units = requested_units if ticket.get("close_partial") else position_units
            average_entry_price = number(matches[0].get("openRate"))
            if average_entry_price <= 0:
                raise ManualOrderError("Prezzo medio di apertura non disponibile.")
            amount = (price * units).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if prepared is not None:
                units = number(prepared["units"])
            estimated_pnl = (price - average_entry_price) * units
            estimated_pnl_percent = (price / average_entry_price - 1) * 100
            ticket = {
                **ticket,
                "amount": str(amount),
                "average_entry_price": str(average_entry_price),
                "estimated_pnl": str(estimated_pnl.quantize(Decimal("0.01"))),
                "estimated_pnl_percent": str(estimated_pnl_percent.quantize(Decimal("0.01"))),
            }
            if units <= 0 or units > position_units:
                raise ManualOrderError("Quantità da vendere superiore alla posizione posseduta.")
            if ticket.get("close_partial") and units >= position_units:
                raise ManualOrderError("Per vendere tutta la posizione usa la chiusura totale.")
            if ticket.get("close_partial") and amount < eligibility.minimum_position:
                raise ManualOrderError(
                    f"La chiusura parziale deve rispettare il minimo eToro: "
                    f"{eligibility.minimum_position} USD."
                )
            endpoint = (
                "https://public-api.etoro.com/api/v1/trading/execution/demo/market-close-orders/positions/"
                + ticket["position_id"]
            )
            # Full close is the safe default. Partial close sends the verified
            # units explicitly and is allowed only when the residual/amount
            # checks above pass.
            payload = {"UnitsToDeduct": float(units) if ticket.get("close_partial") else None}
            ticket = {**ticket, "units": str(units), "close_partial": bool(ticket.get("close_partial"))}
        if price <= 0:
            raise ManualOrderError("Prezzo non valido.")
        return (
            {
                **ticket,
                "account": account,
                "price": str(price),
                "price_as_of": quote.as_of.isoformat(),
            },
            endpoint,
            payload,
            client,
            http,
            credentials,
            identity,
        )

    def preview(self, data):
        ticket = request_order(data)
        checked, *_ = self._check(ticket)
        oid = str(uuid4())
        with self._db() as db:
            db.execute(
                "INSERT INTO manual_orders VALUES (?, ?, ?, 'PREVIEW', NULL)",
                (oid, json.dumps(checked), self.clock().timestamp()),
            )
        return {
            "status": "PREVIEW",
            "preview_id": oid,
            "expires_in": 120,
            "ticket": {k: v for k, v in checked.items() if k != "account"},
            "message": "Controlla il riepilogo e conferma l'ordine Demo.",
        }

    def recent(self, limit=10):
        """Return the local order ledger without making a broker call."""
        with self._db() as db:
            rows = db.execute(
                "SELECT id, ticket, created, state, result FROM manual_orders "
                "ORDER BY created DESC LIMIT ?",
                (max(1, min(int(limit), 25)),),
            ).fetchall()
        orders = []
        for oid, raw_ticket, created, state, raw_result in rows:
            try:
                ticket = json.loads(raw_ticket)
            except (TypeError, ValueError):
                ticket = {}
            try:
                result = json.loads(raw_result) if raw_result else {}
            except (TypeError, ValueError):
                result = {}
            orders.append(
                {
                    "preview_id": str(oid),
                    "side": ticket.get("side"),
                    "symbol": ticket.get("symbol"),
                    "instrument_id": ticket.get("instrument_id"),
                    "position_id": ticket.get("position_id"),
                    "amount": ticket.get("amount"),
                    "units": ticket.get("units"),
                    "status": result.get("status", state),
                    "broker_order_id": result.get("broker_order_id"),
                    "message": result.get("message"),
                    "created_at": datetime.fromtimestamp(float(created), UTC).isoformat(),
                }
            )
        return {"status": "LOCAL_ORDER_LEDGER", "orders": orders, "broker_write_calls": 0}

    def status(self, data):
        if not isinstance(data, dict) or set(data) != {"preview_id"}:
            raise ManualOrderError("Riferimento ordine non valido.")
        with self._db() as db:
            row = db.execute(
                "SELECT ticket, state, result FROM manual_orders WHERE id=?",
                (str(data["preview_id"]),),
            ).fetchone()
        if row is None:
            raise ManualOrderError("Ordine non trovato.")
        result = (
            json.loads(row[2])
            if row[2]
            else {"status": row[1], "message": "Invio in verifica. Non ripetere l'ordine."}
        )
        if result.get("broker_order_id") and row[1] in {"SUBMITTED", "PENDING", "PARTIALLY_FILLED"}:
            client, _, _, settings = self.factory()
            identity = client.identity()
            if (settings.expected_gcid and identity.stable_user_id != settings.expected_gcid) or (
                settings.expected_username
                and (identity.username or "").casefold() != settings.expected_username.casefold()
            ):
                raise ManualOrderError("Identità del conto cambiata.")
            ticket = json.loads(row[0])
            try:
                state = client.demo_order_state(
                    identity, ticket["instrument_id"], result["broker_order_id"]
                ).value
            except EtoroApiError:
                # A broker order ID that is absent from the reconciliation
                # payload is not evidence of a fill. Keep it blocked, but
                # expose the honest state to the UI instead of claiming that
                # the order is still broker-pending.
                state = "UNCONFIRMED"
                result.update(
                    status=state,
                    message=(
                        f"Ordine Demo {result['broker_order_id']} accettato, ma "
                        "eToro non ha riconciliato l'esito. La posizione è ancora aperta; "
                        "non ripetere l'ordine."
                    ),
                )
                with self._db() as db:
                    db.execute(
                        "UPDATE manual_orders SET state=?, result=? WHERE id=?",
                        (state, json.dumps(result), str(data["preview_id"])),
                    )
                return result
            if state != "UNKNOWN":
                result.update(
                    status=state, message=f"Ordine Demo {result['broker_order_id']}: {state}."
                )
                with self._db() as db:
                    db.execute(
                        "UPDATE manual_orders SET state=?, result=? WHERE id=?",
                        (state, json.dumps(result), str(data["preview_id"])),
                    )
        return result

    def submit(self, data):
        if (
            not isinstance(data, dict)
            or set(data) != {"preview_id", "confirmed"}
            or data["confirmed"] is not True
        ):
            raise ManualOrderError("Conferma esplicita del riepilogo richiesta.")
        oid = str(data["preview_id"])
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT ticket, created, state, result FROM manual_orders WHERE id=?", (oid,)
            ).fetchone()
            if row is None:
                raise ManualOrderError("Anteprima non trovata: prepara l'ordine.")
            if row[2] != "PREVIEW":
                return (
                    json.loads(row[3])
                    if row[3]
                    else {
                        "status": "UNKNOWN",
                        "message": "Invio già avviato. Verifica il conto prima di ripetere.",
                    }
                )
            if self.clock().timestamp() - row[1] > 120:
                raise ManualOrderError("Anteprima scaduta: prepara nuovamente l'ordine.")
            ticket = json.loads(row[0])
            pending = db.execute(
                "SELECT id, ticket FROM manual_orders WHERE state IN "
                "('SENDING','UNKNOWN','SUBMITTED','UNCONFIRMED','PENDING','PARTIALLY_FILLED') LIMIT 1"
            ).fetchone()
            pending_ticket = json.loads(pending[1]) if pending else None
            close_after_open = (
                ticket.get("side") == "SELL"
                and pending_ticket is not None
                and pending_ticket.get("side") == "BUY"
            )
            if pending and not close_after_open:
                raise ManualOrderError(
                    "Un ordine precedente è in verifica. "
                    "Controlla il suo esito prima di inviarne un altro."
                )
            checked, endpoint, payload, client, http, credentials, identity = self._check(
                ticket, prepared=ticket
            )
            # Commit the reservation BEFORE the only broker write; a crash never causes a retry.
            db.execute("UPDATE manual_orders SET state='SENDING' WHERE id=?", (oid,))
        result = {
            "preview_id": oid,
            "status": "UNKNOWN",
            "message": "Esito incerto: verifica il conto Demo, non ripetere l'ordine.",
            "broker_write_calls": 1,
        }
        try:
            headers = credentials.headers()
            headers["x-request-id"] = oid
            response = http.post_once(endpoint, headers, payload)
            if 400 <= response.status < 500:
                result.update(
                    status="REJECTED",
                    message=f"Ordine rifiutato da eToro (HTTP {response.status}).",
                )
            elif 200 <= response.status < 300:
                raw = response.json()
                info = raw.get("orderForClose", raw)
                order_id = info.get("orderId", info.get("orderID"))
                if (
                    isinstance(order_id, (str, int))
                    and str(order_id).isdigit()
                    and int(order_id) > 0
                ):
                    result.update(
                        status="SUBMITTED",
                        broker_order_id=str(order_id),
                        message=(
                            f"Ordine Demo inviato a eToro. ID {order_id}. Esecuzione in verifica."
                        ),
                    )
                    try:
                        state = client.demo_order_state(
                            identity, ticket["instrument_id"], str(order_id)
                        ).value
                        if state in {
                            "FILLED",
                            "REJECTED",
                            "CANCELLED",
                            "PENDING",
                            "PARTIALLY_FILLED",
                        }:
                            result.update(status=state, message=f"Ordine Demo {order_id}: {state}.")
                    except Exception:
                        pass
        except Exception:
            pass
        with self._db() as db:
            db.execute(
                "UPDATE manual_orders SET state=?, result=? WHERE id=?",
                (result["status"], json.dumps(result), oid),
            )
        return result


manual_orders = ManualDemoOrders()
