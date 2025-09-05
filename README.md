# Home Assistant Integration for EZVIZ HP7/CP7 Intercom

This is a **custom Home Assistant integration** that adds support for the **EZVIZ HP7 and CP7 video intercoms**.
It allows you to **unlock the door and the gate remotely**, monitor device status, and expose the main functions of the device within your Home Assistant environment.

DO NOT USE THIS INTEGRATION AND THE OFFICIAL ONE TOGETHER (it may not open the door) https://www.home-assistant.io/integrations/ezviz/
---

## ✨ Features

- Discover and register your EZVIZ HP7 or CP7 device automatically.
- Control:
  - 🔑 Unlock **door** (lock #2 by default).
  - 🚪 Unlock **gate** (lock #1 by default).
- Retrieve device information (firmware, version, online status, Wi-Fi signal, etc.).
- Expose useful entities in Home Assistant for automation and dashboards.
- Compatible with **multiple regions** (EU/US).

---

## 📦 Installation via HACS

1. Open Home Assistant  
2. Go to **HACS > Integrations > Custom repositories**  
3. Add: `https://github.com/Bobsilvio/ezviz_hp7` with type `Integration`  
4. Search for `Ezviz Hp7` and install it  
5. Restart Home Assistant  
6. Go to **Settings > Devices & Services** and add the integration

## 📦 Installation simple
[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=bobsilvio&repository=ezviz_hp7&category=integration)

---

---

## ⚙️ Configuration

1. In Home Assistant, go to **Settings → Devices & Services → Add Integration**.
2. Search for **EZVIZ HP7/CP7**.
3. Enter your **EZVIZ account credentials**:
   - **Username** (email used for EZVIZ app login).
   - **Password**.
   - **Region** (usually `eu` for Europe, `us` for North America).

The integration will log in through the EZVIZ API and automatically detect your HP7 or CP7 device.

---

## 🛠 Usage

Once set up, you will see:
- A device card for your **EZVIZ HP7/CP7 intercom**.
- Two services exposed:
  - `ezviz_hp7.unlock_door`
  - `ezviz_hp7.unlock_gate`

These can be used in **automations, scripts, and dashboards**.

Example automation:
```yaml
alias: Unlock gate on RFID card
trigger:
  - platform: state
    entity_id: sensor.rfid_reader
    to: "CARD_1234"
action:
  - service: ezviz_hp7.unlock_gate
    data:
      serial: BE7062577-BE6963574
```

---

## 🚧 Limitations

- **Live video streaming** uses the local RTSP feed of the device. The device must be
  reachable on your network and the integration needs to obtain the verification code
  from EZVIZ.
- The integration currently supports **one device per account** (multi-device support planned).

---

## 🤝 Contributing

Pull requests and issues are welcome!  
If you encounter bugs or want to suggest new features, open an [issue](../../issues).

---

## 📜 License

This project is released under a **proprietary license**.  
It is provided **as-is**, without warranty of any kind.  
You may use it in your personal Home Assistant installation, but redistribution is not permitted without explicit authorization.

---

## ☕ Support the project

If you like this integration and want to support further development:  
[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/silviosmart )

---
