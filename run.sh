#!/usr/bin/env bash
# =============================================================================
# run.sh — Secure Remote Chat CLI — Interactive Management Console
# Usage: bash run.sh   (or ./run.sh after chmod +x run.sh)
# =============================================================================

# ---- Colors & formatting ----
R='\033[0;31m'    # red
G='\033[0;32m'    # green
Y='\033[1;33m'    # yellow
B='\033[0;34m'    # blue
C='\033[0;36m'    # cyan
M='\033[0;35m'    # magenta
W='\033[1;37m'    # bold white
D='\033[2m'       # dim
N='\033[0m'       # reset
BOLD='\033[1m'

# ---- Constants ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/remote-cli"
LOG_DIR="/var/log/remote-cli"
SERVICE_USER="chatcli"
SERVICE="remote-cli"
ENV_FILE="${APP_DIR}/.env"
SUDOERS_FILE="/etc/sudoers.d/chatcli"

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

ok()   { echo -e "  ${G}✓${N}  $*"; }
info() { echo -e "  ${C}→${N}  $*"; }
warn() { echo -e "  ${Y}⚠${N}  $*"; }
err()  { echo -e "  ${R}✗${N}  $*"; }
sep()  { echo -e "  ${D}────────────────────────────────────────────────────────────${N}"; }

press_any_key() {
    echo ""
    echo -ne "  ${D}Press Enter to continue...${N}"
    read -r
}

run_as_root() {
    if [[ $EUID -eq 0 ]]; then
        "$@"
    elif command -v sudo &>/dev/null; then
        sudo "$@"
    else
        err "Root access required. Run the script with: sudo bash run.sh"
        return 1
    fi
}

# Call at the start of any action that needs root.
# Validates sudo once (prompts if needed) and caches the session.
_ensure_sudo() {
    [[ $EUID -eq 0 ]] && return 0
    if ! command -v sudo &>/dev/null; then
        err "sudo not found — run this script as root:  sudo bash run.sh"
        press_any_key
        return 1
    fi
    if ! sudo -v -p "  $(echo -e "${D}[sudo] password for ${USER}:${N} ")" 2>/dev/null; then
        err "Could not acquire sudo privileges."
        err "Run this script as root:  sudo bash run.sh"
        press_any_key
        return 1
    fi
    return 0
}

need_root_warning() {
    if [[ $EUID -ne 0 ]]; then
        echo -e "  ${D}(You may be prompted for your sudo password.)${N}"
    fi
}

# =============================================================================
# STATE DETECTION
# =============================================================================

is_installed() {
    [[ -d "${APP_DIR}" && -x "${APP_DIR}/venv/bin/python" ]]
}

service_status() {
    if ! is_installed; then
        echo "NOT_INSTALLED"; return
    fi
    # Check the unit file exists on disk — most reliable cross-distro test
    if [[ ! -f "/etc/systemd/system/${SERVICE}.service" ]]; then
        echo "NOT_INSTALLED"; return
    fi
    if systemctl is-active --quiet "${SERVICE}" 2>/dev/null; then
        echo "RUNNING"
    elif systemctl is-failed --quiet "${SERVICE}" 2>/dev/null; then
        echo "FAILED"
    elif systemctl is-enabled --quiet "${SERVICE}" 2>/dev/null; then
        echo "STOPPED"
    else
        echo "DISABLED"
    fi
}

has_config() {
    [[ -f "${ENV_FILE}" ]] || return 1
    # The .env is 640 root:chatcli — a regular operator may not be able to read it directly.
    # sudo -n uses the cached session and never prompts, so this is silent.
    local content
    content=$(cat "${ENV_FILE}" 2>/dev/null || sudo -n cat "${ENV_FILE}" 2>/dev/null || true)
    [[ -z "${content}" ]] && return 1
    echo "${content}" | grep -qE '^TELEGRAM_BOT_TOKEN=.{10,}' && \
    echo "${content}" | grep -qE '^ALLOWED_TELEGRAM_USER_IDS=[0-9]'
}

# =============================================================================
# BANNER
# =============================================================================

banner() {
    clear
    echo -e "${C}${BOLD}"
    echo '     ██████╗ ███████╗███╗   ███╗ ██████╗ ████████╗███████╗     ██████╗██╗     ██╗'
    echo '     ██╔══██╗██╔════╝████╗ ████║██╔═══██╗╚══██╔══╝██╔════╝    ██╔════╝██║     ██║'
    echo '     ██████╔╝█████╗  ██╔████╔██║██║   ██║   ██║   █████╗      ██║     ██║     ██║'
    echo '     ██╔══██╗██╔══╝  ██║╚██╔╝██║██║   ██║   ██║   ██╔══╝      ██║     ██║     ██║'
    echo '     ██║  ██║███████╗██║ ╚═╝ ██║╚██████╔╝   ██║   ███████╗    ╚██████╗███████╗██║'
    echo '     ╚═╝  ╚═╝╚══════╝╚═╝     ╚═╝ ╚═════╝    ╚═╝   ╚══════╝     ╚═════╝╚══════╝╚═╝'
    echo -e "${N}${D}                           Secure Telegram Remote CLI  ·  Management Console${N}"
    echo ""

    # ---- Live status bar ----
    local svc_state svc_label cfg_label
    svc_state=$(service_status)

    case "${svc_state}" in
        RUNNING)      svc_label="${G}${BOLD}● RUNNING${N}"      ;;
        FAILED)       svc_label="${R}${BOLD}● FAILED${N}"       ;;
        STOPPED)      svc_label="${Y}${BOLD}● STOPPED${N}"      ;;
        DISABLED)     svc_label="${D}● DISABLED${N}"            ;;
        NOT_INSTALLED) svc_label="${D}○ NOT INSTALLED${N}"      ;;
    esac

    if has_config; then
        cfg_label="${G}✓ Configured${N}"
    else
        cfg_label="${Y}⚠ Not configured${N}"
    fi

    echo -e "  Service: ${svc_label}   Config: ${cfg_label}   ${D}${APP_DIR}${N}"
    echo -e "  ${D}$(date '+%a %b %d %Y  %H:%M:%S %Z')${N}"
    echo ""
    sep
    echo ""
}

# =============================================================================
# MAIN MENU
# =============================================================================

main_menu() {
    while true; do
        banner
        echo -e "  ${W}${BOLD}MAIN MENU${N}"
        echo ""

        echo -e "  ${C}${BOLD}── SERVICE ──────────────────────────────────────────────────${N}"
        if is_installed; then
            echo -e "  ${C}${BOLD}1${N}  ${W}Install / Reinstall${N}   ${D}Overwrite deployment from current source${N}"
        else
            echo -e "  ${G}${BOLD}1  Install               First-time setup${N}"
        fi
        echo -e "  ${C}${BOLD}2${N}  ${W}Status               ${N}  ${D}Service health dashboard${N}"
        echo -e "  ${C}${BOLD}3${N}  ${W}Start                ${N}  ${D}Start the bot${N}"
        echo -e "  ${C}${BOLD}4${N}  ${W}Stop                 ${N}  ${D}Stop the bot${N}"
        echo -e "  ${C}${BOLD}5${N}  ${W}Restart              ${N}  ${D}Restart the bot${N}"
        echo ""

        echo -e "  ${M}${BOLD}── MANAGEMENT ───────────────────────────────────────────────${N}"
        echo -e "  ${M}${BOLD}6${N}  ${W}Logs                 ${N}  ${D}Live logs and audit trail${N}"
        echo -e "  ${M}${BOLD}7${N}  ${W}Config               ${N}  ${D}Edit .env, HOME_DIR, sudo permissions${N}"
        echo -e "  ${M}${BOLD}8${N}  ${W}Update               ${N}  ${D}Sync source, reinstall deps, restart${N}"
        echo ""

        echo -e "  ${R}${BOLD}── DANGER ───────────────────────────────────────────────────${N}"
        echo -e "  ${R}${BOLD}9${N}  ${R}Uninstall            ${N}  ${D}Remove service, files, and user${N}"
        echo ""

        echo -e "  ${D}a  About   0  Exit${N}"
        echo ""
        echo -ne "  ${W}Choice → ${N}"
        read -r choice

        case "${choice}" in
            1) action_install   ;;
            2) action_status    ;;
            3) action_start     ;;
            4) action_stop      ;;
            5) action_restart   ;;
            6) menu_logs        ;;
            7) menu_config      ;;
            8) action_update    ;;
            9) action_uninstall ;;
            a|A) action_about  ;;
            0|q|Q|exit)
                echo ""
                echo -e "  ${D}Goodbye.${N}"
                echo ""
                exit 0
                ;;
            *)
                echo -e "  ${R}Invalid option '${choice}'.${N}"
                sleep 1
                ;;
        esac
    done
}

# =============================================================================
# LOGS SUBMENU
# =============================================================================

menu_logs() {
    while true; do
        banner
        echo -e "  ${W}${BOLD}LOGS${N}"
        echo ""
        echo -e "  ${C}[1]${N}  Live service log       journalctl -u ${SERVICE} -f"
        echo -e "  ${C}[2]${N}  App log (last 100)     ${LOG_DIR}/app.log"
        echo -e "  ${C}[3]${N}  Audit log (last 100)   ${LOG_DIR}/audit.log"
        echo -e "  ${C}[4]${N}  Audit log (live)       tail -f audit.log"
        echo -e "  ${C}[5]${N}  Rejection attempts     grep REJECTED audit.log"
        echo ""
        echo -e "  ${D}[0]  Back${N}"
        echo ""
        echo -ne "  ${W}Choice → ${N}"
        read -r choice

        case "${choice}" in
            1)
                echo ""
                info "Streaming journal (Ctrl+C to stop)..."
                echo ""
                journalctl -u "${SERVICE}" -f --no-pager 2>/dev/null \
                    || warn "journalctl unavailable or service not found"
                press_any_key
                ;;
            2)
                echo ""
                if [[ -f "${LOG_DIR}/app.log" ]]; then
                    tail -n 100 "${LOG_DIR}/app.log"
                else
                    warn "app.log not found — service may not have started yet"
                fi
                press_any_key
                ;;
            3)
                echo ""
                if [[ -f "${LOG_DIR}/audit.log" ]]; then
                    tail -n 100 "${LOG_DIR}/audit.log"
                else
                    warn "audit.log not found — no commands have been run yet"
                fi
                press_any_key
                ;;
            4)
                echo ""
                if [[ -f "${LOG_DIR}/audit.log" ]]; then
                    info "Streaming audit log (Ctrl+C to stop)..."
                    echo ""
                    tail -f "${LOG_DIR}/audit.log"
                else
                    warn "audit.log not found"
                fi
                press_any_key
                ;;
            5)
                echo ""
                if [[ -f "${LOG_DIR}/audit.log" ]]; then
                    local count
                    count=$(grep -c "REJECTED" "${LOG_DIR}/audit.log" 2>/dev/null || echo 0)
                    echo -e "  ${Y}${BOLD}${count} rejection event(s) found${N}"
                    echo ""
                    grep "REJECTED" "${LOG_DIR}/audit.log" | tail -n 100
                else
                    warn "audit.log not found"
                fi
                press_any_key
                ;;
            0|q|Q) return ;;
            *) echo -e "  ${R}Invalid option.${N}"; sleep 1 ;;
        esac
    done
}

# =============================================================================
# CONFIG SUBMENU
# =============================================================================

menu_config() {
    while true; do
        banner
        echo -e "  ${W}${BOLD}CONFIGURATION${N}"
        echo ""

        # Show config file state
        if has_config; then
            ok ".env is configured"
        else
            warn ".env is missing or incomplete"
        fi

        if [[ -f "${SUDOERS_FILE}" ]]; then
            ok "Sudoers allowlist installed at ${SUDOERS_FILE}"
        else
            warn "Sudoers allowlist not installed"
        fi

        echo ""
        echo -e "  ${M}${BOLD}[1]${N}  Setup Wizard          ${BOLD}Enter token + user ID step by step${N}"
        echo -e "  ${M}[2]${N}  Edit .env directly    Open in text editor (advanced)"
        echo -e "  ${M}[3]${N}  Install sudoers       Deploy sudo allowlist template"
        echo -e "  ${M}[4]${N}  Edit sudoers          Modify sudo allowlist"
        echo -e "  ${M}[5]${N}  Show current config   Print .env (token masked)"
        echo -e "  ${M}[6]${N}  Output style          Change Telegram message format"
        echo ""
        echo -e "  ${D}[0]  Back${N}"
        echo ""
        echo -ne "  ${W}Choice → ${N}"
        read -r choice

        case "${choice}" in
            1) _wizard_config   ;;
            2) _edit_env        ;;
            3) _install_sudoers ;;
            4) _edit_sudoers    ;;
            5) _show_env        ;;
            6) _change_style    ;;
            0|q|Q) return       ;;
            *) echo -e "  ${R}Invalid option.${N}"; sleep 1 ;;
        esac
    done
}

_wizard_config() {
    banner
    echo -e "  ${W}${BOLD}SETUP WIZARD${N}"
    echo ""
    echo -e "  ${D}Guided configuration — takes about 2 minutes.${N}"
    echo ""
    sep
    echo ""

    # ---- Read current values (sudo -n = silent, uses cached session) ----
    local current_token="" current_ids="" current_home="" current_format="" current_extra_path=""
    if [[ -f "${ENV_FILE}" ]]; then
        local existing
        existing=$(cat "${ENV_FILE}" 2>/dev/null || sudo -n cat "${ENV_FILE}" 2>/dev/null || true)
        current_token=$(echo "${existing}"      | grep '^TELEGRAM_BOT_TOKEN='           | cut -d= -f2-)
        current_ids=$(echo "${existing}"        | grep '^ALLOWED_TELEGRAM_USER_IDS='    | cut -d= -f2-)
        current_home=$(echo "${existing}"       | grep '^HOME_DIR='                     | cut -d= -f2-)
        current_format=$(echo "${existing}"     | grep '^OUTPUT_FORMAT='                | cut -d= -f2-)
        current_extra_path=$(echo "${existing}" | grep '^EXTRA_PATH='                   | cut -d= -f2-)
    fi

    # =========================================================
    # STEP 1 — Bot Token
    # =========================================================
    echo -e "  ${C}${BOLD}Step 1 of 5${N}  ${W}Telegram Bot Token${N}"
    echo ""
    echo -e "  ${D}How to get your token:${N}"
    echo -e "  ${D}  1. Open Telegram and search for  @BotFather${N}"
    echo -e "  ${D}  2. Send:  /newbot${N}"
    echo -e "  ${D}  3. Choose a name and username for your bot${N}"
    echo -e "  ${D}  4. BotFather will reply with your token — paste it below${N}"
    echo ""
    echo -e "  ${D}  Token format:  1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ${N}"
    echo ""

    if [[ -n "${current_token}" ]]; then
        local masked="${current_token:0:10}...${current_token: -4}"
        echo -e "  Current value: ${D}${masked}${N}"
        echo -e "  ${D}(press Enter to keep the existing token)${N}"
        echo ""
    fi

    local input_token=""
    while true; do
        echo -ne "  ${W}Paste your bot token: ${N}"
        read -r input_token

        # Keep existing if user pressed Enter
        if [[ -z "${input_token}" && -n "${current_token}" ]]; then
            input_token="${current_token}"
            ok "Keeping existing token"
            break
        fi

        # Basic format check: digits, colon, then 35+ alphanumeric chars
        if [[ "${input_token}" =~ ^[0-9]{7,12}:[A-Za-z0-9_-]{35,}$ ]]; then
            ok "Token format looks correct"
            break
        else
            warn "That doesn't look right. Expected format: 1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ"
            echo -e "  ${D}Check you copied the full token from BotFather and try again.${N}"
            echo ""
        fi
    done

    echo ""
    sep
    echo ""

    # =========================================================
    # STEP 2 — Allowed User IDs
    # =========================================================
    echo -e "  ${C}${BOLD}Step 2 of 5${N}  ${W}Your Telegram User ID${N}"
    echo ""
    echo -e "  ${D}How to find your numeric User ID:${N}"
    echo -e "  ${D}  1. Open Telegram and search for  @userinfobot${N}"
    echo -e "  ${D}  2. Send any message to it${N}"
    echo -e "  ${D}  3. It will reply with your  Id:  number — paste it below${N}"
    echo ""
    echo -e "  ${D}  Multiple admins: separate IDs with a comma — 123456789,987654321${N}"
    echo ""

    if [[ -n "${current_ids}" ]]; then
        echo -e "  Current value: ${D}${current_ids}${N}"
        echo -e "  ${D}(press Enter to keep the existing IDs)${N}"
        echo ""
    fi

    local input_ids=""
    while true; do
        echo -ne "  ${W}Paste your User ID: ${N}"
        read -r input_ids

        # Keep existing if user pressed Enter
        if [[ -z "${input_ids}" && -n "${current_ids}" ]]; then
            input_ids="${current_ids}"
            ok "Keeping existing user IDs"
            break
        fi

        # Must be one or more comma-separated integers
        if [[ "${input_ids}" =~ ^[0-9]+(,[[:space:]]*[0-9]+)*$ ]]; then
            # Strip any spaces around commas
            input_ids="${input_ids//[[:space:]]/}"
            ok "User ID format looks correct"
            break
        else
            warn "User IDs must be numbers only — e.g.  123456789  or  123456789,987654321"
            echo -e "  ${D}Make sure you got the Id: number from @userinfobot, not a username.${N}"
            echo ""
        fi
    done

    echo ""
    sep
    echo ""

    # =========================================================
    # STEP 3 — Home Directory
    # =========================================================
    echo -e "  ${C}${BOLD}Step 3 of 5${N}  ${W}Home Directory${N}"
    echo ""
    echo -e "  ${D}This is where  cd ~  and bare  cd  will land you.${N}"
    echo -e "  ${D}Usually /home/your-username  or /root for the root account.${N}"
    echo ""

    # Auto-suggest based on SUDO_USER (the user who ran sudo) or USER
    local suggested_home="/home/${SUDO_USER:-${USER}}"
    if [[ ! -d "${suggested_home}" ]]; then
        suggested_home="/root"
    fi

    if [[ -n "${current_home}" ]]; then
        echo -e "  Current value: ${D}${current_home}${N}"
        echo -e "  ${D}(press Enter to keep it)${N}"
    else
        echo -e "  Suggested: ${D}${suggested_home}${N}  (press Enter to use this)"
    fi
    echo ""

    local input_home=""
    echo -ne "  ${W}Home directory: ${N}"
    read -r input_home

    if [[ -z "${input_home}" ]]; then
        input_home="${current_home:-${suggested_home}}"
        ok "Using ${input_home}"
    elif [[ ! -d "${input_home}" ]]; then
        warn "Directory ${input_home} does not exist yet (will still be saved)"
    else
        ok "Home directory set to ${input_home}"
    fi

    echo ""
    sep
    echo ""

    # =========================================================
    # STEP 4 — Output Format
    # =========================================================
    echo -e "  ${C}${BOLD}Step 4 of 5${N}  ${W}Output Format${N}"
    echo ""
    echo -e "  ${D}How command results look in your Telegram chat:${N}"
    echo ""
    echo -e "  ${W}1${N}  terminal  user@host:cwd\$ classic shell prompt  ${D}(recommended)${N}"
    echo -e "  ${W}2${N}  minimal   Raw output only, no decoration"
    echo -e "  ${W}3${N}  standard  📁 cwd + output + exit code"
    echo -e "  ${W}4${N}  compact   📁 cwd and status on one header line"
    echo -e "  ${W}5${N}  verbose   Hostname + cwd + echoed command + output + timing"
    echo -e "  ${W}6${N}  styled    🟢/🔴 emoji status icons + bold text"
    echo -e "  ${W}7${N}  rich      ━━ border header with hostname, cwd, and command"
    echo ""

    if [[ -n "${current_format}" ]]; then
        echo -e "  Current: ${D}${current_format}${N}  (press Enter to keep it)"
    else
        echo -e "  ${D}(press Enter for  standard)${N}"
    fi
    echo ""

    local input_fmt_num="" input_format=""
    echo -ne "  ${W}Choose [1-7, Enter=terminal]: ${N}"
    read -r input_fmt_num

    case "${input_fmt_num}" in
        1) input_format="terminal" ;;
        2) input_format="minimal"  ;;
        3) input_format="standard" ;;
        4) input_format="compact"  ;;
        5) input_format="verbose"  ;;
        6) input_format="styled"   ;;
        7) input_format="rich"     ;;
        "") input_format="${current_format:-terminal}" ;;
        *)  warn "Invalid choice — using terminal"; input_format="terminal" ;;
    esac
    ok "Format set to '${input_format}'"

    echo ""
    sep
    echo ""

    # =========================================================
    # STEP 5 — Extra PATH
    # =========================================================
    echo -e "  ${C}${BOLD}Step 5 of 5${N}  ${W}Extra PATH${N}"
    echo ""
    echo -e "  ${D}Directories prepended to PATH so user-installed tools are found.${N}"
    echo -e "  ${D}Needed for: claude, npm globals, pip user installs, snap packages.${N}"
    echo ""

    # Auto-suggest based on the home dir entered in step 3
    local suggested_extra="${input_home}/.local/bin:/snap/bin"

    if [[ -n "${current_extra_path}" ]]; then
        echo -e "  Current: ${D}${current_extra_path}${N}"
        echo -e "  ${D}(press Enter to keep it)${N}"
    else
        echo -e "  Suggested: ${D}${suggested_extra}${N}  (press Enter to use this)"
    fi
    echo ""

    local input_extra_path=""
    echo -ne "  ${W}Extra PATH: ${N}"
    read -r input_extra_path

    if [[ -z "${input_extra_path}" ]]; then
        input_extra_path="${current_extra_path:-${suggested_extra}}"
        ok "Using ${input_extra_path}"
    else
        ok "Extra PATH set to ${input_extra_path}"
    fi

    echo ""
    sep
    echo ""

    # =========================================================
    # Write .env
    # =========================================================
    echo -e "  ${W}${BOLD}Saving configuration...${N}"
    echo ""
    need_root_warning

    # Write to a temp file first, then copy with sudo
    local tmpfile
    tmpfile=$(mktemp /tmp/remote-cli-cfg.XXXXXX)

    printf 'TELEGRAM_BOT_TOKEN=%s\nALLOWED_TELEGRAM_USER_IDS=%s\nHOME_DIR=%s\nEXTRA_PATH=%s\nOUTPUT_FORMAT=%s\nCOMMAND_TIMEOUT=10\nMAX_OUTPUT_LINES=50\nMAX_OUTPUT_BYTES=3800\nLOG_DIR=%s\n' \
        "${input_token}" "${input_ids}" "${input_home}" "${input_extra_path}" "${input_format}" "${LOG_DIR}" > "${tmpfile}"

    local write_ok=false
    if run_as_root cp "${tmpfile}" "${ENV_FILE}" 2>/dev/null; then
        run_as_root chown root:"${SERVICE_USER}" "${ENV_FILE}" 2>/dev/null || true
        run_as_root chmod 640 "${ENV_FILE}" 2>/dev/null || true
        write_ok=true
    fi
    rm -f "${tmpfile}"

    if [[ "${write_ok}" == true ]]; then
        ok "Configuration written to ${ENV_FILE}"
    else
        err "Failed to write ${ENV_FILE} — try running run.sh with sudo"
        press_any_key
        return
    fi

    echo ""
    sep
    echo ""
    ok "${BOLD}All done!${N}"
    echo ""

    if is_installed; then
        echo -ne "  Restart the service now to apply changes? ${D}[Y/n]${N}: "
        read -r answer
        [[ "${answer}" =~ ^[Nn]$ ]] || action_restart
    else
        echo -e "  ${D}Return to the main menu and run [1] Install to deploy the service.${N}"
        echo ""
        press_any_key
    fi
}

_change_style() {
    banner
    echo -e "  ${W}${BOLD}OUTPUT STYLE${N}"
    echo ""

    local current_format=""
    if [[ -f "${ENV_FILE}" ]]; then
        local existing
        existing=$(cat "${ENV_FILE}" 2>/dev/null || sudo -n cat "${ENV_FILE}" 2>/dev/null || true)
        current_format=$(echo "${existing}" | grep '^OUTPUT_FORMAT=' | cut -d= -f2-)
    fi

    echo -e "  ${D}How command results look in your Telegram chat:${N}"
    echo ""
    echo -e "  ${W}1${N}  terminal  user@host:cwd\$ classic shell prompt  ${D}(recommended)${N}"
    echo -e "  ${W}2${N}  minimal   Raw output only, no decoration"
    echo -e "  ${W}3${N}  standard  📁 cwd + output + exit code"
    echo -e "  ${W}4${N}  compact   📁 cwd and status on one header line"
    echo -e "  ${W}5${N}  verbose   Hostname + cwd + echoed command + output + timing"
    echo -e "  ${W}6${N}  styled    🟢/🔴 emoji status icons + bold text"
    echo -e "  ${W}7${N}  rich      ━━ border header with hostname, cwd, and command"
    echo ""

    if [[ -n "${current_format}" ]]; then
        echo -e "  Current: ${C}${BOLD}${current_format}${N}  (press Enter to keep it)"
    else
        echo -e "  ${D}(press Enter for  terminal)${N}"
    fi
    echo ""
    echo -ne "  ${W}Choose [1-7, Enter=keep]: ${N}"
    read -r input_fmt_num

    local new_format=""
    case "${input_fmt_num}" in
        1) new_format="terminal" ;;
        2) new_format="minimal"  ;;
        3) new_format="standard" ;;
        4) new_format="compact"  ;;
        5) new_format="verbose"  ;;
        6) new_format="styled"   ;;
        7) new_format="rich"     ;;
        "") new_format="${current_format:-terminal}" ;;
        *)  warn "Invalid choice — keeping current"; new_format="${current_format:-terminal}" ;;
    esac

    need_root_warning

    # Update or append OUTPUT_FORMAT in .env
    if [[ -f "${ENV_FILE}" ]]; then
        local existing
        existing=$(cat "${ENV_FILE}" 2>/dev/null || sudo -n cat "${ENV_FILE}" 2>/dev/null || true)
        if echo "${existing}" | grep -q '^OUTPUT_FORMAT='; then
            local tmpfile
            tmpfile=$(mktemp /tmp/remote-cli-style.XXXXXX)
            echo "${existing}" | sed "s/^OUTPUT_FORMAT=.*/OUTPUT_FORMAT=${new_format}/" > "${tmpfile}"
            run_as_root cp "${tmpfile}" "${ENV_FILE}"
            run_as_root chown root:"${SERVICE_USER}" "${ENV_FILE}" 2>/dev/null || true
            run_as_root chmod 640 "${ENV_FILE}" 2>/dev/null || true
            rm -f "${tmpfile}"
        else
            # OUTPUT_FORMAT not in file yet — append it
            echo "OUTPUT_FORMAT=${new_format}" | run_as_root tee -a "${ENV_FILE}" > /dev/null
        fi
        ok "Style set to '${new_format}'"
    else
        err ".env not found — run the Setup Wizard first"
        press_any_key
        return
    fi

    echo ""
    if is_installed; then
        echo -ne "  Restart service to apply? ${D}[Y/n]${N}: "
        read -r answer
        [[ "${answer}" =~ ^[Nn]$ ]] || action_restart
    fi
}

_edit_env() {
    echo ""
    local target="${ENV_FILE}"

    if [[ ! -f "${target}" ]]; then
        if [[ -f "${SCRIPT_DIR}/.env.example" ]]; then
            warn ".env not found at ${target}"
            echo -ne "  Create from template and edit? ${D}[Y/n]${N}: "
            read -r answer
            [[ "${answer}" =~ ^[Nn]$ ]] && return
            need_root_warning
            run_as_root cp "${SCRIPT_DIR}/.env.example" "${target}"
            run_as_root chown root:"${SERVICE_USER}" "${target}" 2>/dev/null || true
            run_as_root chmod 640 "${target}" 2>/dev/null || true
            ok "Created ${target} from template"
        else
            err ".env.example not found in ${SCRIPT_DIR} — was the project deployed correctly?"
            press_any_key
            return
        fi
    fi

    need_root_warning
    local editor="${VISUAL:-${EDITOR:-nano}}"
    run_as_root "${editor}" "${target}"
    ok "Config saved. Use [5] Restart to apply changes."
    press_any_key
}

_edit_sudoers() {
    echo ""
    if [[ ! -f "${SUDOERS_FILE}" ]]; then
        warn "Sudoers file not found. Run option [3] to install from template first."
        press_any_key
        return
    fi
    need_root_warning
    # visudo -f validates syntax before saving — safer than editing directly
    run_as_root visudo -f "${SUDOERS_FILE}"
    ok "Sudoers updated."
    press_any_key
}

_install_sudoers() {
    echo ""
    local template="${SCRIPT_DIR}/scripts/sudoers_chatcli.example"

    if [[ ! -f "${template}" ]]; then
        err "Template not found at ${template}"
        press_any_key
        return
    fi

    if [[ -f "${SUDOERS_FILE}" ]]; then
        warn "Sudoers file already exists at ${SUDOERS_FILE}."
        echo -ne "  Overwrite with template? ${D}[y/N]${N}: "
        read -r answer
        [[ "${answer}" =~ ^[Yy]$ ]] || return
    fi

    need_root_warning

    # visudo -c validates the file before installing
    if run_as_root visudo -c -f "${template}"; then
        run_as_root cp "${template}" "${SUDOERS_FILE}"
        run_as_root chmod 440 "${SUDOERS_FILE}"
        ok "Sudoers allowlist installed at ${SUDOERS_FILE}"
        echo ""
        warn "Review and tighten the allowlist before starting the service."
        echo -ne "  Open sudoers for editing now? ${D}[Y/n]${N}: "
        read -r answer
        [[ "${answer}" =~ ^[Nn]$ ]] || run_as_root visudo -f "${SUDOERS_FILE}"
    else
        err "Template failed visudo syntax check — not installed."
    fi

    press_any_key
}

_show_env() {
    echo ""
    if [[ ! -f "${ENV_FILE}" ]]; then
        warn "No .env file found at ${ENV_FILE}"
        press_any_key
        return
    fi

    echo -e "  ${D}${ENV_FILE}${N}"
    sep

    # Read line by line; mask the bot token value
    while IFS= read -r line; do
        if [[ "${line}" =~ ^TELEGRAM_BOT_TOKEN= ]]; then
            local key="${line%%=*}"
            echo -e "  ${key}=${Y}[MASKED]${N}"
        elif [[ "${line}" =~ ^# || -z "${line}" ]]; then
            echo -e "  ${D}${line}${N}"
        else
            echo "  ${line}"
        fi
    done < <(run_as_root cat "${ENV_FILE}" 2>/dev/null || cat "${ENV_FILE}" 2>/dev/null || echo "(cannot read file)")

    press_any_key
}

# =============================================================================
# ACTION FUNCTIONS
# =============================================================================

action_install() {
    banner
    echo -e "  ${W}${BOLD}INSTALL${N}"
    echo ""

    _ensure_sudo || return

    if is_installed; then
        warn "Application is already installed at ${APP_DIR}."
        echo -ne "  Reinstall / overwrite? ${D}[y/N]${N}: "
        read -r confirm
        [[ "${confirm}" =~ ^[Yy]$ ]] || return
        echo ""
    fi

    echo ""

    # ---- Step 1: System user ----
    info "Step 1/6  Creating system user '${SERVICE_USER}'..."
    if id "${SERVICE_USER}" &>/dev/null; then
        ok "User '${SERVICE_USER}' already exists — skipping"
    else
        run_as_root useradd \
            --system \
            --shell /sbin/nologin \
            --home-dir "${APP_DIR}" \
            --no-create-home \
            --comment "Remote CLI service account" \
            "${SERVICE_USER}" \
        && ok "User '${SERVICE_USER}' created" \
        || { err "Failed to create user"; press_any_key; return; }
    fi

    # ---- Step 2: Deploy files ----
    info "Step 2/6  Deploying application to ${APP_DIR}..."
    run_as_root mkdir -p "${APP_DIR}"
    run_as_root rsync -a \
        --exclude='.git' \
        --exclude='venv' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.env' \
        "${SCRIPT_DIR}/" "${APP_DIR}/" \
    && ok "Files deployed" \
    || { err "rsync failed"; press_any_key; return; }

    # ---- Step 3: Permissions ----
    info "Step 3/6  Setting ownership and permissions..."
    run_as_root chown -R root:root "${APP_DIR}"
    run_as_root chmod -R o-rwx "${APP_DIR}"
    run_as_root chgrp -R "${SERVICE_USER}" "${APP_DIR}"
    run_as_root chmod -R g+rX "${APP_DIR}"
    # .env: root-owned, readable by chatcli group
    if [[ ! -f "${ENV_FILE}" ]]; then
        run_as_root cp "${APP_DIR}/.env.example" "${ENV_FILE}"
    fi
    run_as_root chown root:"${SERVICE_USER}" "${ENV_FILE}"
    run_as_root chmod 640 "${ENV_FILE}"
    ok "Permissions set"

    # ---- Step 4: Log directory ----
    info "Step 4/6  Creating log directory ${LOG_DIR}..."
    run_as_root mkdir -p "${LOG_DIR}"
    run_as_root chown "${SERVICE_USER}:${SERVICE_USER}" "${LOG_DIR}"
    run_as_root chmod 750 "${LOG_DIR}"
    ok "Log directory ready"

    # ---- Step 5: Python virtual environment ----
    info "Step 5/6  Building Python virtual environment (this may take a moment)..."
    run_as_root python3 -m venv "${APP_DIR}/venv" \
    || { err "python3 -m venv failed — is python3-venv installed?"; press_any_key; return; }
    run_as_root "${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip
    run_as_root "${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt" \
    && ok "Virtual environment ready" \
    || { err "pip install failed — check network connectivity"; press_any_key; return; }
    run_as_root chown -R root:"${SERVICE_USER}" "${APP_DIR}/venv"
    run_as_root chmod -R g+rX "${APP_DIR}/venv"

    # ---- Step 6: systemd ----
    info "Step 6/6  Installing systemd service..."
    run_as_root cp "${APP_DIR}/systemd/remote-cli.service" /etc/systemd/system/
    run_as_root chmod 644 /etc/systemd/system/remote-cli.service
    run_as_root systemctl daemon-reload
    run_as_root systemctl enable "${SERVICE}" \
    && ok "Service installed and enabled" \
    || { err "systemctl enable failed"; press_any_key; return; }

    echo ""
    sep
    echo ""
    ok "${BOLD}Installation complete!${N}"
    echo ""

    # Prompt for next steps
    if ! has_config; then
        warn "You must set your bot token and user IDs before the service will work."
        echo -ne "  Configure now? ${D}[Y/n]${N}: "
        read -r answer
        if [[ ! "${answer}" =~ ^[Nn]$ ]]; then
            _wizard_config
            return
        fi
    fi

    echo -ne "  Install sudoers allowlist now? ${D}[y/N]${N}: "
    read -r answer
    [[ "${answer}" =~ ^[Yy]$ ]] && _install_sudoers

    echo ""
    echo -ne "  Start the service now? ${D}[Y/n]${N}: "
    read -r answer
    [[ "${answer}" =~ ^[Nn]$ ]] || action_start
}

# ---- Status ----
action_status() {
    banner
    echo -e "  ${W}${BOLD}STATUS${N}"
    echo ""

    local svc_state
    svc_state=$(service_status)

    case "${svc_state}" in
        RUNNING)
            echo -e "  ${G}${BOLD}● Service is RUNNING${N}"
            ;;
        FAILED)
            echo -e "  ${R}${BOLD}● Service FAILED${N}"
            warn "Check logs (option [6] → Live service log) for the cause."
            ;;
        STOPPED)
            echo -e "  ${Y}${BOLD}● Service is STOPPED${N}"
            ;;
        NOT_INSTALLED)
            echo -e "  ${D}○ Service is not installed${N}"
            warn "Run option [1] to install."
            press_any_key
            return
            ;;
        DISABLED)
            echo -e "  ${D}● Service is DISABLED${N}"
            ;;
    esac

    echo ""
    sep

    echo ""
    echo -e "  ${D}SYSTEMD${N}"
    echo ""
    systemctl status "${SERVICE}" --no-pager -l 2>/dev/null || warn "systemctl status unavailable"

    echo ""
    sep
    echo ""
    echo -e "  ${D}DISK USAGE${N}"
    echo ""
    du -sh "${APP_DIR}" 2>/dev/null && echo ""
    ls -lh "${LOG_DIR}"/ 2>/dev/null || echo "  (log directory empty or missing)"

    echo ""
    sep
    echo ""
    echo -e "  ${D}LAST 5 AUDIT EVENTS${N}"
    echo ""
    if [[ -f "${LOG_DIR}/audit.log" ]]; then
        tail -n 5 "${LOG_DIR}/audit.log"
    else
        echo -e "  ${D}(no audit events yet)${N}"
    fi

    press_any_key
}

# ---- Start ----
action_start() {
    banner
    echo -e "  ${W}${BOLD}START SERVICE${N}"
    echo ""

    if ! is_installed; then
        err "Service not installed. Run option [1] first."
        press_any_key
        return
    fi

    if ! has_config; then
        warn "Configuration is incomplete (.env missing token or user IDs)."
        echo -ne "  Edit config now? ${D}[Y/n]${N}: "
        read -r answer
        [[ "${answer}" =~ ^[Nn]$ ]] || { _edit_env; return; }
    fi

    _ensure_sudo || return
    info "Starting ${SERVICE}..."
    if run_as_root systemctl start "${SERVICE}"; then
        sleep 1  # give the service a moment to fail-fast if misconfigured
        if systemctl is-active --quiet "${SERVICE}" 2>/dev/null; then
            ok "Service started successfully"
        else
            err "Service started but immediately stopped — check logs (option 6)"
        fi
    else
        err "systemctl start failed"
    fi

    echo ""
    run_as_root systemctl status "${SERVICE}" --no-pager 2>/dev/null || true

    press_any_key
}

# ---- Stop ----
action_stop() {
    banner
    echo -e "  ${W}${BOLD}STOP SERVICE${N}"
    echo ""

    local svc_state
    svc_state=$(service_status)

    if [[ "${svc_state}" == "NOT_INSTALLED" ]]; then
        warn "Service is not installed."
        press_any_key
        return
    fi

    if [[ "${svc_state}" == "STOPPED" || "${svc_state}" == "DISABLED" ]]; then
        warn "Service is already stopped."
        press_any_key
        return
    fi

    _ensure_sudo || return
    info "Stopping ${SERVICE}..."
    run_as_root systemctl stop "${SERVICE}" \
        && ok "Service stopped" \
        || err "Stop command returned an error — check journalctl"

    press_any_key
}

# ---- Restart ----
action_restart() {
    banner
    echo -e "  ${W}${BOLD}RESTART SERVICE${N}"
    echo ""

    if ! is_installed; then
        err "Service not installed. Run option [1] first."
        press_any_key
        return
    fi

    if ! has_config; then
        warn "Configuration may be incomplete — attempting restart anyway."
        echo ""
    fi

    _ensure_sudo || return
    info "Reloading systemd unit files..."
    run_as_root systemctl daemon-reload

    info "Restarting ${SERVICE}..."
    if run_as_root systemctl restart "${SERVICE}"; then
        sleep 2
        if systemctl is-active --quiet "${SERVICE}" 2>/dev/null; then
            ok "Service restarted and running"
        else
            err "Service restarted but is not active — check logs (option 6)"
        fi
    else
        err "systemctl restart failed"
    fi

    echo ""
    run_as_root systemctl status "${SERVICE}" --no-pager 2>/dev/null || true

    press_any_key
}

# ---- Update ----
action_update() {
    banner
    echo -e "  ${W}${BOLD}UPDATE${N}"
    echo ""

    if ! is_installed; then
        err "Application not installed. Run option [1] first."
        press_any_key
        return
    fi

    _ensure_sudo || return

    echo ""
    echo -e "  ${D}What do you want to do?${N}"
    echo ""
    echo -e "  ${C}${BOLD}1${N}  Full update     git pull + deps + restart"
    echo -e "  ${C}${BOLD}2${N}  Apply config    reload .env and restart only  ${D}(use this after editing .env)${N}"
    echo ""
    echo -ne "  Choice ${D}[1/2]${N}: "
    read -r update_choice
    echo ""

    if [[ "${update_choice}" == "2" ]]; then
        # ── Config-reload fast path ───────────────────────────────────
        info "Reading current config from ${ENV_FILE}..."
        if [[ -f "${ENV_FILE}" ]]; then
            local fmt home extra
            fmt=$(grep  -E '^OUTPUT_FORMAT=' "${ENV_FILE}" 2>/dev/null | cut -d= -f2-)
            home=$(grep -E '^HOME_DIR='      "${ENV_FILE}" 2>/dev/null | cut -d= -f2-)
            extra=$(grep -E '^EXTRA_PATH='   "${ENV_FILE}" 2>/dev/null | cut -d= -f2-)
            echo -e "  ${D}OUTPUT_FORMAT=${N}${W}${fmt:-<not set>}${N}"
            echo -e "  ${D}HOME_DIR     =${N}${W}${home:-<not set>}${N}"
            echo -e "  ${D}EXTRA_PATH   =${N}${W}${extra:-<not set>}${N}"
            echo ""
        else
            warn "No .env found at ${ENV_FILE} — restart may fail"
            echo ""
        fi
        info "Reloading systemd unit and restarting service..."
        run_as_root systemctl daemon-reload
        if run_as_root systemctl restart "${SERVICE}"; then
            sleep 2
            if systemctl is-active --quiet "${SERVICE}" 2>/dev/null; then
                ok "Service restarted — new .env values are now active"
            else
                err "Service not active after restart — check logs (option 6)"
            fi
        else
            err "systemctl restart failed — run option [6] to see logs"
        fi
        echo ""
        sep
        ok "${BOLD}Config applied!${N}"
        press_any_key
        return
    fi

    # ── Full update path ─────────────────────────────────────────────
    if [[ -d "${SCRIPT_DIR}/.git" ]]; then
        info "Pulling latest code from git..."
        if git -C "${SCRIPT_DIR}" pull; then
            ok "Source updated"
        else
            warn "git pull failed — deploying current source as-is"
        fi
        echo ""
    else
        info "No git repository found — using current source files"
    fi

    if [[ "$(realpath "${SCRIPT_DIR}")" != "$(realpath "${APP_DIR}")" ]]; then
        info "Syncing files from ${SCRIPT_DIR} to ${APP_DIR}..."
        run_as_root rsync -a \
            --exclude='.git' \
            --exclude='venv' \
            --exclude='__pycache__' \
            --exclude='*.pyc' \
            --exclude='.env' \
            "${SCRIPT_DIR}/" "${APP_DIR}/" \
        && ok "Files synced" \
        || { err "rsync failed"; press_any_key; return; }
    else
        ok "Running from ${APP_DIR} — no rsync needed"
    fi

    # Fix permissions after sync
    run_as_root chown -R root:root "${APP_DIR}"
    run_as_root chmod -R o-rwx "${APP_DIR}"
    run_as_root chgrp -R "${SERVICE_USER}" "${APP_DIR}"
    run_as_root chmod -R g+rX "${APP_DIR}"
    run_as_root chown root:"${SERVICE_USER}" "${ENV_FILE}" 2>/dev/null || true
    run_as_root chmod 640 "${ENV_FILE}" 2>/dev/null || true
    run_as_root chown -R root:"${SERVICE_USER}" "${APP_DIR}/venv" 2>/dev/null || true

    info "Updating Python dependencies..."
    run_as_root "${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip
    run_as_root "${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt" \
    && ok "Dependencies updated" \
    || warn "pip install had errors — check manually"
    run_as_root chmod -R g+rX "${APP_DIR}/venv"

    info "Reloading systemd unit and restarting service..."
    run_as_root systemctl daemon-reload
    if run_as_root systemctl restart "${SERVICE}"; then
        sleep 2
        if systemctl is-active --quiet "${SERVICE}" 2>/dev/null; then
            ok "Service restarted successfully"
        else
            err "Service not active after restart — check logs (option 6)"
        fi
    else
        err "Restart failed — run option [6] to see logs"
    fi

    echo ""
    sep
    ok "${BOLD}Update complete!${N}"

    press_any_key
}

# ---- Uninstall ----
action_uninstall() {
    banner
    echo -e "  ${R}${BOLD}UNINSTALL${N}"
    echo ""
    warn "This will permanently stop and remove the Remote CLI service."
    echo ""
    echo -e "  Type ${BOLD}REMOVE${N} to confirm, or press Enter to cancel:"
    echo -ne "  → "
    read -r confirm

    if [[ "${confirm}" != "REMOVE" ]]; then
        info "Cancelled."
        press_any_key
        return
    fi

    echo ""
    _ensure_sudo || return
    echo ""

    info "Stopping and disabling service..."
    run_as_root systemctl stop "${SERVICE}" 2>/dev/null || true
    run_as_root systemctl disable "${SERVICE}" 2>/dev/null || true
    run_as_root rm -f /etc/systemd/system/remote-cli.service
    run_as_root systemctl daemon-reload
    ok "systemd service removed"

    echo ""
    echo -ne "  Delete application files at ${APP_DIR}? ${D}[y/N]${N}: "
    read -r answer
    if [[ "${answer}" =~ ^[Yy]$ ]]; then
        run_as_root rm -rf "${APP_DIR}"
        ok "Application files deleted"
    fi

    echo -ne "  Delete log files at ${LOG_DIR}? ${D}[y/N]${N}: "
    read -r answer
    if [[ "${answer}" =~ ^[Yy]$ ]]; then
        run_as_root rm -rf "${LOG_DIR}"
        ok "Log files deleted"
    fi

    echo -ne "  Remove sudoers file ${SUDOERS_FILE}? ${D}[y/N]${N}: "
    read -r answer
    if [[ "${answer}" =~ ^[Yy]$ ]]; then
        run_as_root rm -f "${SUDOERS_FILE}"
        ok "Sudoers file removed"
    fi

    echo -ne "  Delete system user '${SERVICE_USER}'? ${D}[y/N]${N}: "
    read -r answer
    if [[ "${answer}" =~ ^[Yy]$ ]]; then
        run_as_root userdel "${SERVICE_USER}" 2>/dev/null \
            && ok "User '${SERVICE_USER}' removed" \
            || warn "Could not remove user — may still have running processes"
    fi

    echo ""
    sep
    ok "${BOLD}Uninstall complete.${N}"

    press_any_key
}

# ---- About ----
action_about() {
    banner
    echo -e "  ${W}${BOLD}ABOUT${N}"
    echo ""
    sep
    echo ""
    echo -e "  ${C}${BOLD}Secure Remote Chat CLI${N}"
    echo -e "  ${D}A hardened Telegram bot for remote Linux administration over WireGuard.${N}"
    echo ""
    sep
    echo ""
    echo -e "  ${W}${BOLD}AUTHOR${N}"
    echo ""
    echo -e "  ${BOLD}Maor${N}  ${D}aka${N}  ${C}${BOLD}akamaor${N}"
    echo ""
    echo -e "  ${D}DevSecOps Engineer · Algorithm Engineer · Python Developer${N}"
    echo ""
    echo -e "  ${D}GitHub   ${N}${B}github.com/akamaor${N}"
    echo -e "  ${D}Email    ${N}${B}akamaor@gmail.com${N}"
    echo ""
    sep
    echo ""
    echo -e "  ${W}${BOLD}PROJECT${N}"
    echo ""
    echo -e "  ${D}Built with a strict security-first philosophy:${N}"
    echo ""
    echo -e "  ${G}✓${N}  ${D}Outbound long-polling only — zero open inbound ports${N}"
    echo -e "  ${G}✓${N}  ${D}shell=False execution — no shell injection surface${N}"
    echo -e "  ${G}✓${N}  ${D}Silent drop on unauthorized users — no bot fingerprinting${N}"
    echo -e "  ${G}✓${N}  ${D}SIGKILL process groups on timeout — no hanging orphans${N}"
    echo -e "  ${G}✓${N}  ${D}Rotating audit trail — commands logged, stdout never is${N}"
    echo -e "  ${G}✓${N}  ${D}Unprivileged service user + systemd hardening directives${N}"
    echo ""
    sep
    echo ""
    echo -e "  ${D}Architecture is chat-provider-agnostic.${N}"
    echo -e "  ${D}Telegram is the first integration — Signal and WhatsApp adapters planned.${N}"
    echo ""
    press_any_key
}

# =============================================================================
# ENTRY POINT
# =============================================================================

# Dependency check before showing menu
_missing=()
for _cmd in python3 rsync systemctl; do
    command -v "${_cmd}" &>/dev/null || _missing+=("${_cmd}")
done
if [[ ${#_missing[@]} -gt 0 ]]; then
    echo -e "${R}[ERROR]${N} Missing required tools: ${_missing[*]}"
    echo       "        Install with: sudo apt install ${_missing[*]}"
    exit 1
fi

main_menu
