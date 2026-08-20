(() => {
  const MANUAL_VALUE = "__manual__";
  const boundRoots = new WeakSet();
  let cachedPorts = null;
  let pendingRequest = null;

  function escapeAttribute(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll('"', "&quot;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
  }

  function translate(key, fallback) {
    return window.AHAI18n?.t?.(key, fallback) || fallback;
  }

  function markup(options = {}) {
    const value = escapeAttribute(options.value || "");
    const label = escapeAttribute(options.label || translate("task.hardware_serial_device", "Serial device"));
    const detecting = escapeAttribute(translate("task.hardware_serial_detecting", "Detecting serial devices…"));
    const refresh = escapeAttribute(translate("task.hardware_serial_refresh", "Refresh"));
    const manual = escapeAttribute(translate("task.hardware_serial_manual", "Manual input…"));
    const placeholder = escapeAttribute(translate("task.hardware_serial_manual_placeholder", "COM3 or /dev/ttyUSB0"));
    return `
      <label class="field-label hardware-serial-picker" data-hardware-serial-picker>
        <span>${label}</span>
        <div class="hardware-serial-picker-row">
          <select data-hardware-serial-select aria-label="${label}">
            <option value="${value}" selected>${value || detecting}</option>
            <option value="${MANUAL_VALUE}">${manual}</option>
          </select>
          <button type="button" class="button-ghost hardware-serial-refresh" data-hardware-serial-refresh title="${refresh}" aria-label="${refresh}">${refresh}</button>
        </div>
        <input type="hidden" data-hardware-group-field="serial.device" value="${value}">
        <input class="hardware-serial-manual" data-hardware-serial-manual value="${value}" placeholder="${placeholder}" hidden>
        <span class="field-help" data-hardware-serial-state></span>
      </label>`;
  }

  function normalizedPorts(data) {
    const source = Array.isArray(data?.ports) ? data.ports : [];
    const seen = new Set();
    return source.flatMap(item => {
      const device = String(item?.device || "").trim();
      if (!device || seen.has(device)) return [];
      seen.add(device);
      return [{
        device,
        description: String(item?.description || "").trim(),
        hwid: String(item?.hwid || "").trim()
      }];
    });
  }

  function portLabel(port) {
    if (port.description && port.description !== port.device) return `${port.device} — ${port.description}`;
    return port.device;
  }

  function setManualMode(picker, enabled) {
    const manualInput = picker.querySelector("[data-hardware-serial-manual]");
    if (manualInput) manualInput.hidden = !enabled;
  }

  function populatePicker(picker, ports) {
    const select = picker.querySelector("[data-hardware-serial-select]");
    const valueInput = picker.querySelector('[data-hardware-group-field="serial.device"]');
    const manualInput = picker.querySelector("[data-hardware-serial-manual]");
    const state = picker.querySelector("[data-hardware-serial-state]");
    if (!select || !valueInput || !manualInput) return;
    const current = String(valueInput.value || "").trim();
    const detected = ports.some(port => port.device === current);
    select.innerHTML = "";

    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = ports.length
      ? translate("task.hardware_serial_choose", "Choose a detected serial device")
      : translate("task.hardware_serial_empty", "No serial devices detected");
    select.appendChild(placeholder);
    for (const port of ports) {
      const option = document.createElement("option");
      option.value = port.device;
      option.textContent = portLabel(port);
      if (port.hwid) option.title = port.hwid;
      select.appendChild(option);
    }
    const manualOption = document.createElement("option");
    manualOption.value = MANUAL_VALUE;
    manualOption.textContent = translate("task.hardware_serial_manual", "Manual input…");
    select.appendChild(manualOption);

    if (current && detected) {
      select.value = current;
      setManualMode(picker, false);
    } else if (current) {
      select.value = MANUAL_VALUE;
      manualInput.value = current;
      setManualMode(picker, true);
    } else {
      select.value = "";
      manualInput.value = "";
      setManualMode(picker, false);
    }
    if (state) {
      state.textContent = ports.length
        ? translate("task.hardware_serial_detected", `${ports.length} serial device(s) detected`).replace("{count}", String(ports.length))
        : translate("task.hardware_serial_manual_help", "Refresh or use manual input for undetected devices.");
    }
  }

  function populateRoot(root, ports) {
    root?.querySelectorAll?.("[data-hardware-serial-picker]").forEach(picker => populatePicker(picker, ports));
  }

  function setLoading(root, loading) {
    root?.querySelectorAll?.("[data-hardware-serial-refresh]").forEach(button => {
      button.setAttribute("aria-busy", loading ? "true" : "false");
    });
  }

  async function refresh(root, options = {}) {
    if (!options.force && cachedPorts) {
      populateRoot(root, cachedPorts);
      return cachedPorts;
    }
    if (!pendingRequest) {
      const request = fetch("/api/hardware/serial-ports", {
        cache: "no-store",
        headers: { Accept: "application/json" }
      })
        .then(response => (response.ok ? response.json() : { ports: [] }))
        .then(data => {
          cachedPorts = normalizedPorts(data);
          return cachedPorts;
        })
        .catch(() => {
          cachedPorts = [];
          return cachedPorts;
        })
        .finally(() => {
          if (pendingRequest === request) pendingRequest = null;
        });
      pendingRequest = request;
    }
    setLoading(root, true);
    const ports = await pendingRequest;
    setLoading(root, false);
    populateRoot(root, ports);
    return ports;
  }

  function bind(root) {
    if (!root || boundRoots.has(root)) return;
    boundRoots.add(root);
    root.addEventListener("change", event => {
      const select = event.target instanceof Element ? event.target.closest("[data-hardware-serial-select]") : null;
      if (!select) return;
      const picker = select.closest("[data-hardware-serial-picker]");
      const valueInput = picker?.querySelector('[data-hardware-group-field="serial.device"]');
      const manualInput = picker?.querySelector("[data-hardware-serial-manual]");
      if (!picker || !valueInput || !manualInput) return;
      const manual = select.value === MANUAL_VALUE;
      setManualMode(picker, manual);
      valueInput.value = manual ? String(manualInput.value || "").trim() : select.value;
      if (manual) manualInput.focus();
    });
    root.addEventListener("input", event => {
      const manualInput = event.target instanceof Element ? event.target.closest("[data-hardware-serial-manual]") : null;
      if (!manualInput) return;
      const picker = manualInput.closest("[data-hardware-serial-picker]");
      const valueInput = picker?.querySelector('[data-hardware-group-field="serial.device"]');
      if (valueInput) valueInput.value = String(manualInput.value || "").trim();
    });
    root.addEventListener("click", event => {
      const button = event.target instanceof Element ? event.target.closest("[data-hardware-serial-refresh]") : null;
      if (!button) return;
      void refresh(root, { force: true });
    });
  }

  function mount(root) {
    if (!root) return;
    bind(root);
    if (!root.querySelector?.("[data-hardware-serial-picker]")) return;
    if (cachedPorts) populateRoot(root, cachedPorts);
    else void refresh(root);
  }

  window.AHAHardwareSerialPortPicker = Object.freeze({ markup, mount, refresh });
})();
