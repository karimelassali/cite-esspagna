"""Notification-only ICPPlus appointment checker using Playwright & Stealth.

The checker deliberately stops before any appointment is selected or booked.
It uses Playwright with playwright-stealth to bypass client-side bot detection,
waits for the F5 BIG-IP WAF challenge (TSPD) to settle, and supports optional
residential proxy routing for cloud/CI environments.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
from playwright_stealth import Stealth

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Windows PowerShell legacy code page fix
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("cita-tracker")

BASE_URL = "https://icp.administracionelectronica.gob.es/icpplus"
PROVINCE_CODE = os.getenv("PROVINCE_CODE", "48").strip()
PROCEDURE_TEXT = os.getenv(
    "PROCEDURE_TEXT",
    "POLICÍA-TOMA DE HUELLAS (EXPEDICIÓN DE TARJETA) INICIAL, RENOVACIÓN, DUPLICADO Y LEY 14/2013",
).strip()
NIE = os.getenv("NIE", "").strip().upper()
FULL_NAME = os.getenv("FULL_NAME", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
WAIT_SECONDS = int(os.getenv("WAIT_SECONDS", "25"))
ALWAYS_NOTIFY = os.getenv("ALWAYS_NOTIFY", "true").strip().casefold() in {"1", "true", "yes", "on"}
ARTIFACTS = Path(os.getenv("ARTIFACTS_DIR", "artifacts"))
PROXY_SERVER = os.getenv("PROXY_SERVER", "").strip()
BROWSERLESS_TOKEN = os.getenv("BROWSERLESS_TOKEN", "").strip()

NO_SLOT_PATTERNS = (
    "no hay citas disponibles",
    "en este momento no hay citas disponibles",
    "no existen citas disponibles",
)
SLOT_PATTERNS = (
    "seleccione una cita",
    "seleccione la cita",
    "citas disponibles",
    "seleccione una fecha",
    "seleccione el horario",
)


def normalized(value: str) -> str:
    s = unicodedata.normalize("NFKD", str(value)).encode("ASCII", "ignore").decode("utf-8")
    return " ".join(re.sub(r"[^a-zA-Z0-9]+", " ", s.lower()).split())


def get_telegram_token() -> str:
    return os.getenv("TELEGRAM_TOKEN", "").strip() or TELEGRAM_TOKEN


def get_chat_ids() -> list[str]:
    raw = os.getenv("CHAT_IDS", "").strip()
    if not raw:
        raw = os.getenv("CHAT_ID", "").strip()
    if not raw:
        return []
    tokens = re.split(r"[,;\s]+", raw)
    seen = set()
    result = []
    for token in tokens:
        item = token.strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def validate_configuration(require_telegram: bool = True) -> None:
    missing = []
    for name, value in (
        ("NIE", NIE),
        ("FULL_NAME", FULL_NAME),
        ("PROCEDURE_TEXT", PROCEDURE_TEXT),
    ):
        if not value:
            missing.append(name)
    if require_telegram:
        chat_ids = get_chat_ids()
        if not get_telegram_token():
            missing.append("TELEGRAM_TOKEN")
        if not chat_ids:
            missing.append("CHAT_IDS or CHAT_ID")
    if missing:
        raise ValueError("Missing required environment variables: " + ", ".join(missing))
    if not re.fullmatch(r"[XYZ]\d{7}[A-Z]", NIE):
        raise ValueError("NIE must look like X/Y/Z followed by 7 digits and one letter")


def send_telegram(message: str, photo_path: Path | str | None = None) -> None:
    token = get_telegram_token()
    chat_ids = get_chat_ids()
    if not token or not chat_ids:
        raise ValueError("TELEGRAM_TOKEN and at least one chat ID (CHAT_IDS or CHAT_ID) must be set before sending a Telegram message")

    successful = 0
    failed_chats = []

    for chat_id in chat_ids:
        sent = False
        if photo_path and Path(photo_path).exists():
            endpoint = f"https://api.telegram.org/bot{token}/sendPhoto"
            try:
                with open(photo_path, "rb") as photo_file:
                    response = requests.post(
                        endpoint,
                        data={"chat_id": chat_id, "caption": message[:1024]},
                        files={"photo": photo_file},
                        timeout=30,
                    )
                payload = response.json() if response.ok else {}
                if response.ok and payload.get("ok"):
                    LOG.info("Telegram photo proof sent successfully to chat %s", chat_id)
                    sent = True
                else:
                    LOG.warning("sendPhoto failed for chat %s (%s); falling back to sendMessage", chat_id, payload.get("description"))
            except Exception as exc:
                LOG.warning("Could not send Telegram photo to chat %s: %s", chat_id, exc)

        if not sent:
            endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
            try:
                response = requests.post(
                    endpoint,
                    json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
                    timeout=20,
                )
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                if response.ok and payload.get("ok"):
                    LOG.info("Telegram message sent successfully to chat %s", chat_id)
                    sent = True
                else:
                    description = payload.get("description", f"HTTP {response.status_code}")
                    LOG.warning("Telegram rejected message for chat %s: %s", chat_id, description)
                    failed_chats.append((chat_id, description))
            except Exception as exc:
                LOG.warning("Failed to send Telegram message to chat %s: %s", chat_id, exc)
                failed_chats.append((chat_id, str(exc)))

        if sent:
            successful += 1

    if successful == 0:
        descriptions = [f"{cid}: {err}" for cid, err in failed_chats]
        raise RuntimeError("Telegram message failed for all configured chats: " + "; ".join(descriptions))


def parse_proxy_settings():
    if not PROXY_SERVER:
        return None
    parsed = urlparse(PROXY_SERVER)
    server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}" if parsed.port else f"{parsed.scheme}://{parsed.hostname}"
    proxy_dict = {"server": server}
    if parsed.username:
        proxy_dict["username"] = parsed.username
    if parsed.password:
        proxy_dict["password"] = parsed.password
    return proxy_dict


def create_browser(p, headless: bool, proxy_config: dict | None) -> Browser:
    if BROWSERLESS_TOKEN:
        ws_url = (
            f"wss://production-ams.browserless.io/chromium/stealth"
            f"?token={BROWSERLESS_TOKEN}&proxy=residential&proxyCountry=es"
        )
        LOG.info("Connecting to Browserless (Amsterdam) with Spanish residential proxy...")
        return p.chromium.connect_over_cdp(ws_url)

    launch_kwargs = {
        "headless": headless,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    if proxy_config:
        launch_kwargs["proxy"] = proxy_config
        LOG.info("Using proxy server: %s", proxy_config.get("server"))

    return p.chromium.launch(**launch_kwargs)


def create_context(browser: Browser) -> BrowserContext:
    return browser.new_context(
        ignore_https_errors=True,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="es-ES",
        timezone_id="Europe/Madrid",
        viewport={"width": 1920, "height": 1080},
    )


def save_diagnostics(page: Page, label: str) -> None:
    try:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-z0-9_-]+", "-", label.casefold()).strip("-")
        try:
            page.screenshot(path=str(ARTIFACTS / f"{safe}.png"), timeout=5000)
        except Exception as shot_err:
            LOG.warning("Could not capture screenshot for %s: %s", label, shot_err)
        (ARTIFACTS / f"{safe}.html").write_text(page.content(), encoding="utf-8")
        LOG.info("Saved diagnostics in %s", ARTIFACTS.resolve())
    except Exception as exc:
        LOG.warning("Failed saving diagnostics for %s: %s", label, exc)


def setup_page_routes(context: BrowserContext) -> None:
    """Block hanging analytics/telemetry scripts to improve reliability in CI/cloud environments."""
    def block_unnecessary_resources(route):
        url = route.request.url.lower()
        if any(tracker in url for tracker in ("google-analytics.com", "ga.js", "analytics.js")):
            route.abort()
        else:
            route.continue_()
    context.route("**/*", block_unnecessary_resources)


def navigate_page(page: Page, url: str, timeout: int = 90000, max_retries: int = 3) -> None:
    """Navigate to URL with retry and commit fallback."""
    for attempt in range(1, max_retries + 1):
        try:
            LOG.info("Navigating to: %s (attempt %d/%d, timeout %ds)", url, attempt, max_retries, timeout // 1000)
            page.goto(url, timeout=timeout, wait_until="commit")
            return
        except Exception as exc:
            if attempt == max_retries:
                if "timeout" in str(exc).lower():
                    raise RuntimeError(
                        f"Page navigation timed out after {timeout // 1000}s. "
                        "GitHub Actions cloud IPs (Microsoft Azure) are geoblocked / packet-dropped "
                        "by the Spanish Government F5 firewall. "
                        "To run in GitHub Actions, add a Spanish proxy to PROXY_SERVER secret, "
                        "or run the script directly on your computer."
                    ) from exc
                raise
            wait_sec = 4 * attempt
            LOG.warning("Navigation attempt %d failed (%s); retrying in %ds...", attempt, exc, wait_sec)
            page.wait_for_timeout(wait_sec * 1000)


def is_waf_rejected(page: Page) -> bool:
    try:
        title = page.title().lower()
        if "request rejected" in title:
            return True
        body = page.locator("body").inner_text(timeout=2000).lower()
        if "the requested url was rejected" in body:
            return True
    except Exception:
        pass
    return False


def ensure_not_rejected(page: Page) -> None:
    """Check if the WAF rejected the request."""
    if is_waf_rejected(page):
        save_diagnostics(page, "waf-rejected")
        raise RuntimeError(
            "ICPPlus F5 WAF rejected this request (Request Rejected). "
            "Please check if your IP is rate-limited or configure a Spanish residential proxy via PROXY_SERVER."
        )


def wait_for_challenge_settle(page: Page, timeout: int = WAIT_SECONDS) -> None:
    """Wait for F5 BIG-IP TSPD cookie challenge to finish and the form to appear."""
    start = time.time()
    while time.time() - start < timeout:
        ensure_not_rejected(page)
        # Check if the actual application page has loaded
        if page.locator("select#sede, select[name*='tramiteGrupo'], #prov_selecc").count() > 0:
            return
        if page.locator("#txtIdCitado, input[name*='IdCitado']").count() > 0:
            return
        page.wait_for_timeout(1000)
    ensure_not_rejected(page)


def select_procedure(page: Page) -> None:
    target = normalized(PROCEDURE_TEXT)

    # Search through all select dropdowns
    for sel in page.locator("select").all():
        for opt in sel.locator("option").all():
            opt_text = normalized(opt.inner_text())
            if not opt_text:
                continue
            if target in opt_text or opt_text in target or ("toma de huellas" in target and "toma de huellas" in opt_text):
                val = opt.get_attribute("value")
                sel_id = sel.get_attribute("id") or sel.get_attribute("name") or "select"
                sel.select_option(value=val)
                LOG.info("Selected procedure in %s: %s (value=%s)", sel_id, opt.inner_text().strip(), val)

                # Trigger onchange handler and reset other tramite Grupo dropdowns
                page.evaluate(
                    """(currentId) => {
                        const sel = document.getElementById(currentId) || document.querySelector(`select[name='${currentId}']`);
                        if (sel && sel.onchange) sel.onchange();
                        document.querySelectorAll("select[id^='tramiteGrupo']").forEach(s => {
                            if (s.id !== currentId && s.name !== currentId) {
                                s.value = "-1";
                            }
                        });
                        if (window.cargaMensajesTramite) window.cargaMensajesTramite();
                    }""",
                    sel_id,
                )
                page.wait_for_timeout(2000)
                return

    # Radio button fallback
    for label in page.locator("label").all():
        label_text = normalized(label.inner_text())
        if target in label_text or label_text in target or ("toma de huellas" in target and "toma de huellas" in label_text):
            ctrl_id = label.get_attribute("for")
            if ctrl_id and page.locator(f"#{ctrl_id}").count() > 0:
                page.locator(f"#{ctrl_id}").check()
            else:
                label.locator("input[type=radio]").check()
            LOG.info("Selected procedure radio: %s", label.inner_text().strip())
            page.wait_for_timeout(1000)
            return

    raise RuntimeError(f"No procedure contains {PROCEDURE_TEXT!r}")


def fill_identity(page: Page) -> None:
    ensure_not_rejected(page)

    # Document type: NIE radio if present
    nie_radio = page.locator("#rdbTipoDocNie, input[type='radio'][value='NIE']").first
    if nie_radio.is_visible():
        nie_radio.check()
        page.wait_for_timeout(500)

    # NIE input
    id_field = page.locator("#txtIdCitado, input[name='txtIdCitado'], input[id*='IdCitado']").first
    id_field.wait_for(state="visible", timeout=WAIT_SECONDS * 1000)
    id_field.fill(NIE)
    page.wait_for_timeout(300)

    # Full name input
    name_field = page.locator("#txtDesCitado, #txtNombre, input[name='txtDesCitado'], input[id*='Nombre']").first
    name_field.wait_for(state="visible", timeout=WAIT_SECONDS * 1000)
    name_field.fill(FULL_NAME)
    page.wait_for_timeout(300)

    # Nationality
    nationality = os.getenv("NATIONALITY", "MARRUECOS").strip()
    norm_nat = normalized(nationality)
    matched_select = None
    matched_value = None

    for sel in page.locator("select").all():
        if not sel.is_visible():
            continue
        for opt in sel.locator("option").all():
            if normalized(opt.inner_text()) == norm_nat:
                matched_select = sel
                matched_value = opt.get_attribute("value")
                break
        if matched_select:
            break

    if not matched_select:
        raise RuntimeError(f"Could not find nationality dropdown for {nationality!r}")

    matched_select.select_option(value=matched_value)
    LOG.info("Selected nationality: %s", nationality)
    page.wait_for_timeout(500)


def dismiss_cookie_banner(page: Page) -> None:
    cookie_btn = page.locator("#cookie_action_close_header").first
    if cookie_btn.is_visible():
        cookie_btn.click()
        page.wait_for_timeout(500)


def check_appointments() -> bool:
    stealth = Stealth(navigator_languages_override=("es-ES", "es"))
    headless = os.getenv("HEADLESS", "true").lower() != "false"
    proxy_config = parse_proxy_settings()

    with stealth.use_sync(sync_playwright()) as p:
        browser: Browser = create_browser(p, headless, proxy_config)
        try:
            context: BrowserContext = create_context(browser)
            setup_page_routes(context)
            page: Page = context.new_page()
            page.set_default_timeout(90000)
            page.set_default_navigation_timeout(90000)
            stealth.apply_stealth_sync(page)

            query = urlencode({"p": PROVINCE_CODE, "locale": "es"})
            url = f"{BASE_URL}/citar?{query}"
            navigate_page(page, url)

            # 1. Wait for page/F5 challenge to settle
            wait_for_challenge_settle(page)
            dismiss_cookie_banner(page)

            # 2. Wait for scripts and select procedure
            try:
                page.wait_for_function("() => typeof window.eliminarSeleccionOtrosGrupos === 'function'", timeout=15000)
            except Exception:
                pass
            select_procedure(page)

            # 3. Click Aceptar / Siguiente
            submit_btn = page.locator("#btnAceptar, #btnSiguiente, input[type=button][value='Aceptar']").first
            submit_btn.wait_for(state="visible", timeout=WAIT_SECONDS * 1000)
            submit_btn.hover()
            page.wait_for_timeout(500)
            with page.expect_navigation(timeout=WAIT_SECONDS * 1000):
                submit_btn.click()

            page.wait_for_timeout(3000)
            ensure_not_rejected(page)

            # 4. Handle informational / entry intermediate page if present (acInfo)
            entry_btn = page.locator(
                "#btnEntrar, input[value*='Entrar'], button:has-text('Entrar'), div:has-text('sin Cl@ve'), a:has-text('Entrar')"
            ).first
            try:
                if entry_btn.is_visible() or page.locator("#btnEntrar").count() > 0:
                    target_btn = page.locator("#btnEntrar").first if page.locator("#btnEntrar").count() > 0 else entry_btn
                    target_btn.wait_for(state="visible", timeout=10000)
                    LOG.info("Detected intermediate notice page (acInfo). Entering via 'Presentación sin Cl@ve'...")
                    target_btn.scroll_into_view_if_needed()
                    page.wait_for_timeout(3000)
                    with page.expect_navigation(timeout=WAIT_SECONDS * 1000):
                        target_btn.click()
                    page.wait_for_timeout(3000)
                    ensure_not_rejected(page)
            except Exception as e:
                LOG.warning("Intermediate notice check finished: %s", e)

            # 5. Fill identity data
            fill_identity(page)

            # 6. Submit identity form
            id_submit = page.locator("#btnEnviar, #btnAceptar, input[type=submit], input[value='Aceptar'], input[value='Enviar']").first
            id_submit.wait_for(state="visible", timeout=WAIT_SECONDS * 1000)
            id_submit.hover()
            page.wait_for_timeout(500)
            with page.expect_navigation(timeout=WAIT_SECONDS * 1000):
                id_submit.click()
            page.wait_for_timeout(3000)
            ensure_not_rejected(page)

            # 7. Intermediate "Solicitar Cita" button before results, if present (acValidarEntrada)
            solicitar_btn = page.locator(
                "#btnEnviar, input[value*='Solicitar Cita'], button:has-text('Solicitar Cita'), input[value='Solicitar Cita']"
            ).first
            try:
                if solicitar_btn.is_visible() or page.locator("input[value='Solicitar Cita']").count() > 0:
                    target_solicitar = page.locator("input[value='Solicitar Cita']").first if page.locator("input[value='Solicitar Cita']").count() > 0 else solicitar_btn
                    target_solicitar.wait_for(state="visible", timeout=10000)
                    LOG.info("Detected appointment action menu (acValidarEntrada). Clicking 'Solicitar Cita'...")
                    target_solicitar.scroll_into_view_if_needed()
                    page.wait_for_timeout(4000)
                    with page.expect_navigation(timeout=WAIT_SECONDS * 1000):
                        target_solicitar.click()
                    page.wait_for_timeout(3000)
                    ensure_not_rejected(page)
            except Exception as e:
                LOG.warning("Solicitar Cita step finished: %s", e)

            # 8. Check result
            proof_file = ARTIFACTS / "proof.png"
            ARTIFACTS.mkdir(parents=True, exist_ok=True)
            try:
                page.screenshot(path=str(proof_file), timeout=5000)
                LOG.info("Proof screenshot saved to %s", proof_file)
            except Exception as shot_err:
                LOG.warning("Could not capture proof screenshot: %s", shot_err)

            body_text = normalized(page.locator("body").inner_text(timeout=5000))
            if any(pattern in body_text for pattern in NO_SLOT_PATTERNS):
                LOG.info("ICPPlus reports no appointments available")
                return False

            if any(pattern in body_text for pattern in SLOT_PATTERNS):
                save_diagnostics(page, "slot-found")
                LOG.warning("Appointment availability detected!")
                return True

            save_diagnostics(page, "unexpected-page")
            raise RuntimeError("ICPPlus returned an unrecognized page; no alert was sent")

        except Exception:
            try:
                save_diagnostics(page, "error")
            except Exception:
                pass
            raise
        finally:
            browser.close()


def list_procedures() -> int:
    """Open the configured province page and print available procedure labels."""
    stealth = Stealth(navigator_languages_override=("es-ES", "es"))
    headless = os.getenv("HEADLESS", "true").lower() != "false"
    proxy_config = parse_proxy_settings()

    with stealth.use_sync(sync_playwright()) as p:
        browser = create_browser(p, headless, proxy_config)
        try:
            context = create_context(browser)
            setup_page_routes(context)
            page = context.new_page()
            page.set_default_timeout(90000)
            page.set_default_navigation_timeout(90000)
            stealth.apply_stealth_sync(page)

            query = urlencode({"p": PROVINCE_CODE, "locale": "es"})
            url = f"{BASE_URL}/citar?{query}"
            navigate_page(page, url)
            wait_for_challenge_settle(page)
            ensure_not_rejected(page)

            title = page.title().strip()
            options = []
            for opt in page.locator("select option").all():
                label = " ".join(opt.inner_text().split())
                if label and label not in options:
                    options.append(label)

            print(f"Title: {title}")
            print(f"URL: {page.url}")
            print(f"Procedures found ({len(options)}):")
            for label in options:
                print(f"- {label}")

            save_diagnostics(page, "procedure-list")
            return 0 if options else 2
        except Exception as exc:
            LOG.exception("Procedure inspection failed: %s", exc)
            try:
                save_diagnostics(page, "procedure-list-error")
            except Exception:
                pass
            return 1
        finally:
            browser.close()


def verify_procedure_selection() -> int:
    """Verify ICPPlus's procedure-selection step without submitting personal data."""
    stealth = Stealth(navigator_languages_override=("es-ES", "es"))
    headless = os.getenv("HEADLESS", "true").lower() != "false"
    proxy_config = parse_proxy_settings()

    with stealth.use_sync(sync_playwright()) as p:
        browser = create_browser(p, headless, proxy_config)
        try:
            context = create_context(browser)
            setup_page_routes(context)
            page = context.new_page()
            page.set_default_timeout(90000)
            page.set_default_navigation_timeout(90000)
            stealth.apply_stealth_sync(page)

            query = urlencode({"p": PROVINCE_CODE, "locale": "es"})
            navigate_page(page, f"{BASE_URL}/citar?{query}")
            wait_for_challenge_settle(page)
            ensure_not_rejected(page)

            select_procedure(page)
            print("Procedure selection verified successfully.")
            save_diagnostics(page, "procedure-selection-verified")
            return 0
        except Exception as exc:
            LOG.exception("Procedure-selection verification failed: %s", exc)
            try:
                save_diagnostics(page, "procedure-selection-error")
            except Exception:
                pass
            return 1
        finally:
            browser.close()


def test_telegram_notification() -> int:
    """Send a live test message to verify the Telegram bot credentials and connection."""
    validate_configuration(require_telegram=True)
    chat_ids = get_chat_ids()
    chat_ids_str = ", ".join(chat_ids)
    msg = (
        "🔔 إشعار تجريبي من بوت Cita Zarwal!\n\n"
        f"• Trámite: {PROCEDURE_TEXT}\n"
        f"• Provincia: {PROVINCE_CODE}\n"
        f"• Chat IDs: {chat_ids_str}\n\n"
        "✅ البوت خدام ومربوط مزيان مع التيليغرام ديالك دابا!"
    )
    proof_file = ARTIFACTS / "proof.png"
    photo_to_send = proof_file if proof_file.exists() else None
    send_telegram(msg, photo_path=photo_to_send)
    print("Notification sent successfully to Telegram in Moroccan Darija! Check your chat.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list-procedures",
        action="store_true",
        help="inspect the configured province page without submitting personal data",
    )
    parser.add_argument(
        "--verify-procedure",
        action="store_true",
        help="verify procedure selection without submitting personal data",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="send an actual live test notification to verify your Telegram setup",
    )
    args = parser.parse_args()
    if args.test_telegram:
        return test_telegram_notification()
    if args.list_procedures:
        return list_procedures()
    if args.verify_procedure:
        return verify_procedure_selection()
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        try:
            validate_configuration()
            has_slot = check_appointments()
            proof_file = ARTIFACTS / "proof.png"
            photo_proof = proof_file if proof_file.exists() else None

            if has_slot:
                url = f"{BASE_URL}/citar?{urlencode({'p': PROVINCE_CODE, 'locale': 'es'})}"
                send_telegram(
                    "🚨 كاينا موعد متاح دابا! (Cita disponible) 🚨\n\n"
                    f"📋 Trámite: {PROCEDURE_TEXT}\n"
                    f"📍 Provincia: {PROVINCE_CODE}\n\n"
                    f"🔗 دخل ريزيرفي دغيا من هنا بيدك قبل ما يعمرو:\n{url}",
                    photo_path=photo_proof,
                )
                LOG.info("Telegram alert sent")
            elif ALWAYS_NOTIFY:
                send_telegram(
                    "ℹ️ تحديث Cita Zarwal\n\n"
                    f"مازال ما كاينين حتى مواعيد دابا لهاد الإجراء (No hay citas disponibles):\n{PROCEDURE_TEXT}\n\n"
                    "البوت مازال متبع، وغير يتفتح شي موعد غانصيفطو ليك إشعار دغيا إن شاء الله.",
                    photo_path=photo_proof,
                )
                LOG.info("No-availability status sent")
            return 0
        except Exception as exc:
            if "request rejected" in str(exc).lower() and attempt < max_attempts:
                LOG.warning("Encountered transient WAF rate limit; waiting 15s before attempt %d...", attempt + 1)
                time.sleep(15)
                continue

            LOG.error("Check failed (%s): %s. Availability UNKNOWN.", type(exc).__name__, exc)
            proof_file = ARTIFACTS / "proof.png"
            waf_file = ARTIFACTS / "waf-rejected.png"
            photo_proof = proof_file if proof_file.exists() else (waf_file if waf_file.exists() else None)
            if get_telegram_token() and get_chat_ids():
                try:
                    err_str = str(exc).lower()
                    if "request rejected" in err_str:
                        send_telegram(
                            "⚠️ Cita Zarwal: جدار الحماية (F5 WAF) ديال السيت رفض الطلب مؤقتاً (Request Rejected).\n\n"
                            "💡 هادشي كيكون بلوك مؤقت (10 إلى 15 دقيقة) حيت السيت الإسباني كيدير حماية من الضغط وكثرة الطلبات.\n"
                            "🔄 ما تحتاج دير والو، البوت غادي يعاود المحاولة تلقائياً فـ الدورة الجاية بـ IP إسباني جديد.",
                            photo_path=photo_proof,
                        )
                elif "timeout" in err_str or "timed out" in err_str:
                    if not BROWSERLESS_TOKEN and not PROXY_SERVER:
                        send_telegram(
                            "⚠️ Cita Zarwal: السيت ما جاوبش (Timeout) حيت السكريبت ما لقاش BROWSERLESS_TOKEN فـ GitHub Secrets!\n\n"
                            "الحل السريع:\n"
                            "1. دخل لـ GitHub ديالك: Settings -> Secrets and variables -> Actions\n"
                            "2. ضيف New repository secret سميتو بالضبط:\n"
                            "BROWSERLESS_TOKEN\n"
                            "وحط فيه الـ Token ديال Browserless.",
                            photo_path=photo_proof,
                        )
                    else:
                        send_telegram(
                            "⚠️ Cita Zarwal: السيت تعطل فـ الجواب (Timeout).\n"
                            "البوت غادي يعاود المحاولة تلقائياً فـ الدورة الجاية.",
                            photo_path=photo_proof,
                        )
                else:
                    send_telegram(
                        "⚠️ Cita Zarwal: ما قدرناش نتحققو من توفر المواعيد دابا (Estado desconocido).\n"
                        "عافاك دخل شوف السيت ديال ICPPlus بيدك باش تتأكد.",
                        photo_path=photo_proof,
                    )
            except Exception:
                LOG.error("Could not deliver the failure notification")
        return 1


if __name__ == "__main__":
    sys.exit(main())
