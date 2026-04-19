# HA Zhiyun Light

Home Assistant integration to control Zhiyun light devices via BLE,
Based on [`ZhiyunGATTController.swift`](https://github.com/makerwolf/Light-Bridge/blob/main/Light%20Bridge/Managers/ZhiyunGATTController.swift)
and initially rewritten by Claude 4.7 Opus. Tested with **Zhiyun Molux X100**
using **Home Assistant 2026.3.1**.

## Installation

* Install the integration to Home Assistant:
  * Option 1: add via HACS with [custom repository](https://www.hacs.xyz/docs/faq/custom_repositories/)
  * Option 2: copy the contents of `custom_components/zhiyun_ble/`
    to `<your config dir>/custom_components/zhiyun_ble/`.
* Restart Home Assistant
* Available devices will be automatically configured via Bluetooth discovery

## Acknowledgement

Original communication code written by [**@makerwolf**](https://github.com/makerwolf),
see [Light-Bridge](https://github.com/makerwolf/Light-Bridge/)
