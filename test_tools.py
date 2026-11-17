# test_tools.py
import json
from agent.tools import (
    list_services,
    get_logs,
    inspect_container,
    api_probe,
    dispatch,
    TOOL_REGISTRY,
)


def main():
    print("=== 1. list_services ===")
    r = list_services()
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print(f"total = {r.get('total')}")
    assert r["ok"], "list_services 失败"
    assert r["total"] == 5, f"期望 5 个服务，实际 {r['total']}"

    print("\n=== 2. get_logs ===")
    r = get_logs("netbox", tail=20)
    print(json.dumps(r, ensure_ascii=False, indent=2)[:800])

    print("\n=== 3. inspect_container ===")
    r = inspect_container("netbox-docker-netbox-1")
    print(json.dumps(r, ensure_ascii=False, indent=2)[:800])

    print("\n=== 4. api_probe ===")
    r = api_probe()
    print(json.dumps(r, ensure_ascii=False, indent=2)[:800])

    print("\n=== 5. dispatch 入口 ===")
    print(json.dumps(dispatch("list_services"), ensure_ascii=False)[:200])

    print("\n=== 6. 注册表 ===")
    print(list(TOOL_REGISTRY.keys()))

    print("\n全部通过 ✅")


if __name__ == "__main__":
    main()
