/* SOYAL Lovelace dashboard. All values come from Home Assistant state. */
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
export function eventRows(state) {
  return (Array.isArray(state?.attributes?.events) ? state.attributes.events : [])
    .filter((e) => e && typeof e === "object")
    .slice().sort((a, b) => Number(b.id) - Number(a.id)).slice(0, 100);
}
export function lockStatus(output, hold) {
  if (output === "on") return hold === "on" ? "常開（解鎖輸出中）" : "解鎖輸出中";
  if (output === "off") return "上鎖（解鎖輸出關閉）";
  return "狀態未知";
}
function eventTime(event) {
  if (event.event_time_local) return { text: event.event_time_local.replace("T", " "), label: "控制器時間" };
  const date = new Date(event.received_at);
  return { text: Number.isNaN(date.getTime()) ? "時間未知" : date.toLocaleString(), label: "接收時間" };
}
const types = { card_access: "卡片事件", egress: "出門事件", pin_access: "密碼事件", unknown_event: "未知事件" };
export class SoyalAccessCard extends HTMLElement {
  constructor() { super(); this.attachShadow({ mode: "open" }); this.pending = false; }
  setConfig(config) { this.config = { title: "大門", ...config }; this.render(); }
  set hass(hass) { this._hass = hass; this.render(); }
  getCardSize() { return 10; }
  static getConfigElement() { return document.createElement("soyal-access-card-editor"); }
  static getStubConfig() { return { title: "大門" }; }
  state(key) { return this._hass?.states?.[this.config?.[key]]; }
  async control(enabled) {
    if (this.pending || !this.config?.hold_open) return;
    this.pending = true; this.error = ""; this.render();
    try {
      await this._hass.callService("switch", enabled ? "turn_on" : "turn_off",
        { entity_id: this.config.hold_open });
    } catch (err) { this.error = err.message || String(err); }
    finally { this.pending = false; this.render(); }
  }
  eventHTML(event) {
    const time = eventTime(event);
    const person = event.user_address == null ? "使用者未知" : `使用者位址 ${event.user_address}`;
    return `<div class="event"><div><time>${escapeHTML(time.text)}</time><small>${time.label}</small></div>
      <strong>${escapeHTML(person)}</strong><div><span class="badge">${escapeHTML(types[event.event_type] || event.event_type || "未知事件")}</span>
      <small>通行結果未確認</small></div><span>Port ${escapeHTML(event.port ?? "未知")}</span></div>`;
  }
  render() {
    if (!this.config || !this._hass) return;
    const output = this.state("unlock_output")?.state;
    const hold = this.state("hold_open")?.state;
    const node = this.state("node_online")?.state;
    const tcp = this.state("gateway_online")?.state;
    const history = this.state("events");
    const rows = eventRows(history);
    const ready = hold === "on" || hold === "off" || hold === "unknown";
    const connected = node === "on" && tcp === "on";
    const contact = this.state("door_contact")?.state;
    const historyReady = history && !["unknown", "unavailable"].includes(history.state);
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;color:var(--primary-text-color,#eef1f5);font-family:var(--paper-font-body1_-_font-family,system-ui)}
      *{box-sizing:border-box}ha-card{padding:24px;border-radius:24px;background:var(--ha-card-background,var(--card-background-color,#191f27))}
      h1{margin:0 0 22px;font-size:28px}h2{font-size:19px;margin:0 0 16px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
      section{border:1px solid var(--divider-color,#343d47);border-radius:20px;padding:24px;background:linear-gradient(125deg,rgba(135,161,186,.07),transparent)}
      .name{font-size:30px}.connection{margin-top:12px;color:${connected ? "#70d46a" : "#e9af67"}}.status{font-size:28px;color:${output === "on" ? "#70d46a" : "var(--primary-text-color,#eef1f5)" };margin:16px 0}
      .line,.mode{display:flex;align-items:center;justify-content:space-between;gap:16px}.line{padding:16px 0;border-top:1px solid var(--divider-color,#343d47)}
      button{background:var(--primary-color,#39a5ef);color:var(--text-primary-color,#fff);border:0;border-radius:12px;padding:13px 18px;cursor:pointer;font:inherit}
      button:disabled{opacity:.45;cursor:default}button[aria-pressed=true]{background:#278636}.actions{display:flex;gap:10px;flex-wrap:wrap}
      small{display:block;color:var(--secondary-text-color,#adb7c5);font-size:12px;margin-top:6px}.history{grid-column:1/-1}.event{display:grid;grid-template-columns:1.6fr 1.2fr 1.4fr .6fr;gap:20px;align-items:center;padding:18px 0;border-bottom:1px solid var(--divider-color,#343d47)}
      .badge{display:inline-block;background:rgba(111,156,201,.16);padding:6px 10px;border-radius:20px}.empty{padding:30px 0;color:var(--secondary-text-color,#adb7c5)}.error{color:var(--error-color,#ff7676);margin-top:14px}
      .latest .event{grid-template-columns:1fr;border:0}.latest strong{font-size:25px}header{display:flex;justify-content:space-between;gap:14px}
      @media(max-width:650px){ha-card{padding:14px}.grid{grid-template-columns:1fr}section{padding:18px}.event{grid-template-columns:1fr 1fr;gap:12px}.history{grid-column:auto}h1{font-size:24px}.mode{align-items:flex-start;flex-direction:column}}
    </style><ha-card><h1>門禁（SOYAL）</h1><div class="grid">
      <section><div class="name">${escapeHTML(this.config.title)}</div><div class="connection">${connected ? "● 連線正常" : "● 連線未確認"}</div>
      <small>Gateway TCP ${tcp === "on" ? "已連線" : tcp === "off" ? "離線" : "未知"} · Node ${node === "on" ? "在線" : node === "off" ? "離線" : "未知"}</small></section>
      <section><div class="mode"><div><h2>常開模式</h2><small>顯示最後成功命令</small></div><div class="actions">
      <button id="on" aria-pressed="${hold === "on"}" ${!ready || this.pending ? "disabled" : ""}>啟用常開</button>
      <button id="off" aria-pressed="${hold === "off"}" ${!ready || this.pending ? "disabled" : ""}>解除常開</button></div></div>
      ${this.error ? `<div class="error" role="alert">${escapeHTML(this.error)}</div>` : ""}</section>
      <section><h2>門鎖狀態</h2><div class="status">${lockStatus(output, hold)}</div><small>控制器解鎖輸出，實體鎖舌位置須現場確認</small>
      <div class="line"><span>門磁</span><span>${contact === "on" ? "開啟" : contact === "off" ? "關閉" : "未提供／未知"}</span></div>
      <div class="line"><span>Relay／解鎖輸出</span><span>${output === "on" ? "ON（解鎖中）" : output === "off" ? "OFF" : "未知"}</span></div></section>
      <section class="latest"><h2>最後一筆門禁事件</h2>${historyReady && rows.length ? this.eventHTML(rows[0]) : '<div class="empty">尚無紀錄或資料暫不可用</div>'}</section>
      <section class="history"><header><h2>最近通行紀錄</h2><span>${historyReady ? `共 ${escapeHTML(history.state)} 筆` : "資料暫不可用"}</span></header>
      ${historyReady && rows.length ? rows.map(e => this.eventHTML(e)).join("") : '<div class="empty">尚無可顯示的紀錄</div>'}</section>
      </div></ha-card>`;
    this.shadowRoot.getElementById("on").onclick = () => this.control(true);
    this.shadowRoot.getElementById("off").onclick = () => this.control(false);
  }
}
class SoyalAccessEditor extends HTMLElement {
  setConfig(config) { this.config = config; this.render(); }
  set hass(hass) { this._hass = hass; this.render(); }
  render() {
    if (!this.config || !this._hass) return;
    this.replaceChildren();
    const fields = { title: "設備名稱", gateway_online: "Gateway 連線實體", node_online: "Node 在線實體", hold_open: "常開 Switch", unlock_output: "解鎖輸出實體", events: "最近門禁事件 Sensor", door_contact: "門磁實體（可選，on 表示開啟）" };
    for (const [key, label] of Object.entries(fields)) {
      const wrapper = document.createElement("label"); wrapper.style.display = "block"; wrapper.style.margin = "14px 0"; wrapper.textContent = label;
      const input = document.createElement("input"); input.style.cssText = "display:block;width:100%;padding:12px;box-sizing:border-box"; input.value = this.config[key] || "";
      input.onchange = () => { this.config = { ...this.config, [key]: input.value.trim() }; this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: this.config }, bubbles: true, composed: true })); };
      wrapper.append(input); this.append(wrapper);
    }
  }
}
if (!customElements.get("soyal-access-card")) customElements.define("soyal-access-card", SoyalAccessCard);
if (!customElements.get("soyal-access-card-editor")) customElements.define("soyal-access-card-editor", SoyalAccessEditor);
window.customCards = window.customCards || [];
window.customCards.push({ type: "soyal-access-card", name: "SOYAL 門禁儀表板", description: "即時門禁控制與 FIFO 紀錄" });
