import json
import argparse
import subprocess
from urllib.parse import urlparse, parse_qs, unquote


def parse_vless(url: str, port: int):
    u = urlparse(url.strip())
    qs = parse_qs(u.query)

    def q(name, default=""):
        return qs.get(name, [default])[0]

    name = unquote(u.fragment or f"proxy-{port}")

    inbound_tag = f"http-in-{port}"
    outbound_tag = f"vless-out-{port}"

    inbound = {
        "tag": inbound_tag,
        "listen": "127.0.0.1",
        "port": port,
        "protocol": "http",
        "settings": {}
    }

    user = {
        "id": u.username,
        "encryption": q("encryption", "none")
    }

    flow = q("flow")
    if flow:
        user["flow"] = flow

    outbound = {
        "tag": outbound_tag,
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": u.hostname,
                    "port": u.port,
                    "users": [user]
                }
            ]
        },
        "streamSettings": {
            "network": q("type", "tcp"),
            "security": q("security", "none")
        }
    }

    security = q("security")

    if security == "reality":
        outbound["streamSettings"]["realitySettings"] = {
            "serverName": q("sni"),
            "fingerprint": q("fp", "chrome"),
            "publicKey": q("pbk"),
            "shortId": q("sid")
        }

    elif security == "tls":
        outbound["streamSettings"]["tlsSettings"] = {
            "serverName": q("sni")
        }

    rule = {
        "type": "field",
        "inboundTag": [inbound_tag],
        "outboundTag": outbound_tag
    }

    return name, inbound, outbound, rule


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", help="包含多个 vless:// 链接的文件")
    parser.add_argument("--start-port", type=int, default=7890)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--xray", default="xray")
    args = parser.parse_args()

    with open(args.file, "r", encoding="utf-8") as f:
        links = [
            line.strip()
            for line in f
            if line.strip().startswith("vless://")
        ]

    if not links:
        raise RuntimeError("文件中没有找到 vless:// 链接")

    inbounds = []
    outbounds = []
    rules = []

    proxy_addrs = []

    for idx, link in enumerate(links):
        port = args.start_port + idx
        name, inbound, outbound, rule = parse_vless(link, port)

        inbounds.append(inbound)
        outbounds.append(outbound)
        rules.append(rule)

        proxy_addrs.append(f"http://127.0.0.1:{port}")

    config = {
        "log": {
            "loglevel": "warning"
        },
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {
            "rules": rules
        }
    }

    with open(args.config, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    for addr in proxy_addrs:
        print(addr)

    subprocess.run(
        [args.xray, "run", "-config", args.config],
        check=True
    )


if __name__ == "__main__":
    main()
