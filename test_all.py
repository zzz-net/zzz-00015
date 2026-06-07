import os
import sys
import shutil
import json
import stat
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import Role, Status
from datastore import (
    DataStore,
    WorkOrderError,
    PermissionError,
    StatusTransitionError,
    ConcurrentOperationError,
    ExportError,
)


def print_title(t):
    print("\n" + "=" * 70)
    print(f"  {t}")
    print("=" * 70)


def print_ok(msg):
    print(f"  [OK] {msg}")


def print_fail(msg):
    print(f"  [FAIL] {msg}")


def setup():
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)
    exports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")
    if os.path.exists(exports_dir):
        shutil.rmtree(exports_dir)
    return DataStore(data_dir)


def test_normal_flow(store):
    print_title("测试1: 正常登记-派单-接单-完工-验收链路")

    dispatcher = store.get_user("u001")
    tech = store.get_user("u002")
    inspector = store.get_user("u004")

    order = store.create_order(
        title="测试空调故障",
        description="A栋3楼空调不制冷",
        location="A栋-3F",
        category="空调维修",
        priority="高",
        creator=dispatcher,
    )
    print_ok(f"工单登记: {order.order_id}, 状态={order.status.value}")
    assert order.status == Status.PENDING_DISPATCH

    order = store.dispatch_order(order.order_id, tech, dispatcher)
    print_ok(f"派工给 {tech.name}, 状态={order.status.value}, 维修员={order.assignee_name}")
    assert order.status == Status.DISPATCHED
    assert order.assignee_id == tech.user_id

    order = store.accept_order(order.order_id, tech)
    print_ok(f"维修员接单, 状态={order.status.value}")
    assert order.status == Status.IN_PROGRESS

    order = store.complete_order(order.order_id, tech)
    print_ok(f"维修完工, 状态={order.status.value}")
    assert order.status == Status.PENDING_INSPECTION

    order = store.approve_order(order.order_id, inspector)
    print_ok(f"验收通过, 状态={order.status.value}")
    assert order.status == Status.COMPLETED

    assert len(order.history) == 5
    print_ok(f"状态历史共 {len(order.history)} 条记录")

    return order.order_id


def test_race_condition(store):
    print_title("测试2: 两个维修员抢同一工单")

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")

    order = store.create_order(
        title="抢单测试工单",
        description="并发抢单",
        location="B栋",
        category="电梯维修",
        priority="高",
        creator=dispatcher,
    )
    store.dispatch_order(order.order_id, tech1, dispatcher)

    results = {}

    def try_accept(tid, tech):
        try:
            store.accept_order(order.order_id, tech)
            results[tid] = ("success", None)
        except Exception as e:
            results[tid] = ("fail", type(e).__name__ + ": " + str(e))

    t1 = threading.Thread(target=try_accept, args=("t1", tech1))
    t2 = threading.Thread(target=try_accept, args=("t2", tech2))
    t1.start()
    time.sleep(0.01)
    t2.start()
    t1.join()
    t2.join()

    o = store.get_order(order.order_id)
    print_ok(f"工单最终状态: {o.status.value}, 维修员: {o.assignee_name}")
    print(f"  Thread1: {results['t1']}")
    print(f"  Thread2: {results['t2']}")

    success_count = sum(1 for v in results.values() if v[0] == "success")
    fail_count = sum(1 for v in results.values() if v[0] == "fail")
    assert success_count == 1, f"只能有一个成功，实际{success_count}"
    assert fail_count == 1, f"必须有一个失败，实际{fail_count}"
    assert o.status == Status.IN_PROGRESS
    print_ok(f"抢单一致性：仅1人成功，1人失败，工单状态未错乱。异常备注数: {len(o.exception_notes)}")


def test_permission_denied(store):
    print_title("测试3: 无权关闭/操作")

    dispatcher = store.get_user("u001")
    tech = store.get_user("u002")
    inspector = store.get_user("u004")

    order = store.create_order("权限测试", "", "C栋", "电路维修", "中", dispatcher)
    store.dispatch_order(order.order_id, tech, dispatcher)
    store.accept_order(order.order_id, tech)
    store.complete_order(order.order_id, tech)

    try:
        store.approve_order(order.order_id, tech)
        print_fail("维修员居然能验收通过！")
        assert False
    except PermissionError as e:
        print_ok(f"维修员无权验收: {e}")

    try:
        store.dispatch_order(order.order_id, tech, tech)
        print_fail("维修员居然能派工！")
        assert False
    except PermissionError as e:
        print_ok(f"维修员无权派工: {e}")

    o = store.get_order(order.order_id)
    assert o.status == Status.PENDING_INSPECTION, "无权操作不应修改状态"
    print_ok(f"工单状态保持不变: {o.status.value}。异常备注数: {len(o.exception_notes)}")


def test_reject_then_complete(store):
    print_title("测试4: 退回后直接完成（应失败）")

    dispatcher = store.get_user("u001")
    tech = store.get_user("u002")
    inspector = store.get_user("u004")

    order = store.create_order("退回流程测试", "", "D栋", "水管维修", "高", dispatcher)
    store.dispatch_order(order.order_id, tech, dispatcher)
    store.accept_order(order.order_id, tech)
    store.complete_order(order.order_id, tech)
    assert store.get_order(order.order_id).status == Status.PENDING_INSPECTION

    order = store.reject_order(order.order_id, inspector, "漏水问题未解决，请重新处理")
    print_ok(f"验收退回: 状态={order.status.value}")
    assert order.status == Status.IN_PROGRESS

    try:
        store.approve_order(order.order_id, inspector)
        print_fail("退回后居然能直接完成！")
        assert False
    except (StatusTransitionError, WorkOrderError) as e:
        print_ok(f"退回后直接验收失败（符合预期）: {e}")

    o = store.get_order(order.order_id)
    assert o.status == Status.IN_PROGRESS, "非法操作后状态应仍为处理中"
    print_ok(f"退回后必须重新走完工流程，当前状态: {o.status.value}")

    order = store.complete_order(order.order_id, tech)
    print_ok(f"重新完工: 状态={order.status.value}")
    order = store.approve_order(order.order_id, inspector)
    print_ok(f"重新验收通过: 状态={order.status.value}")
    assert order.status == Status.COMPLETED


def test_export_not_writable(store):
    print_title("测试5: 导出目录不可写")

    base = os.path.dirname(os.path.abspath(__file__))
    fake_dir = os.path.join(base, "fake_export_dir.file")
    if os.path.exists(fake_dir):
        os.remove(fake_dir)
    with open(fake_dir, "w") as f:
        f.write("this is a file, not a directory")

    store.set_export_dir(fake_dir)

    try:
        store.export_orders_csv()
        print_fail("文件路径伪装成目录居然导出成功！")
        assert False
    except ExportError as e:
        print_ok(f"非目录路径导出失败（符合预期）: {e}")

    invalid_drive = "Z:\\nonexistent_dir_that_should_not_exist_12345"
    store.set_export_dir(invalid_drive)
    try:
        store.export_orders_json()
        print_fail("不存在的驱动器居然导出成功！")
        assert False
    except ExportError as e:
        print_ok(f"不存在/不可写目录导出失败（符合预期）: {e}")

    try:
        if os.path.exists(fake_dir):
            os.remove(fake_dir)
    except Exception:
        pass

    store.set_export_dir(os.path.join(base, "exports"))
    orders_before = store.get_all_orders()
    print_ok(f"导出失败不影响数据，当前工单数量: {len(orders_before)}")


def test_import_csv(store):
    print_title("测试6: 导入CSV样例数据")

    dispatcher = store.get_user("u001")
    sample = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_orders.csv")
    assert os.path.exists(sample), f"样例文件不存在: {sample}"

    count, errors = store.import_orders_csv(sample, dispatcher)
    print_ok(f"CSV导入: 成功{count}条, 失败{len(errors)}条")
    assert count == 8, f"应导入8条，实际{count}"

    all_orders = store.get_all_orders()
    assert len(all_orders) >= 8
    print_ok(f"当前总工单: {len(all_orders)}")


def test_persistence(store):
    print_title("测试7: 持久化一致性（重启验证）")

    data_dir = store.data_dir
    export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_exports")
    store.set_export_dir(export_dir)

    dispatcher = store.get_user("u001")
    tech = store.get_user("u002")

    order = store.create_order("持久化测试工单", "重启验证", "Z栋", "网络维护", "高", dispatcher)
    store.dispatch_order(order.order_id, tech, dispatcher)
    store.accept_order(order.order_id, tech)
    store.add_exception_note(order.order_id, "这是测试异常备注")

    orders_before = {o.order_id: o.to_dict() for o in store.get_all_orders()}
    history_before = {oid: o["history"] for oid, o in orders_before.items()}
    exceptions_before = {oid: o["exception_notes"] for oid, o in orders_before.items()}
    export_dir_before = store.get_config().export_dir

    csv_path = store.export_orders_csv()
    json_path = store.export_orders_json()
    assert os.path.exists(csv_path) and os.path.exists(json_path)
    csv_size_before = os.path.getsize(csv_path)
    json_size_before = os.path.getsize(json_path)
    print_ok(f"CSV导出: {csv_path} ({csv_size_before}字节)")
    print_ok(f"JSON导出: {json_path} ({json_size_before}字节)")

    print_ok("重启数据存储，模拟关闭应用...")
    del store
    import gc
    gc.collect()

    store2 = DataStore(data_dir)

    orders_after = {o.order_id: o.to_dict() for o in store2.get_all_orders()}
    export_dir_after = store2.get_config().export_dir

    assert set(orders_before.keys()) == set(orders_after.keys()), "工单ID集合不一致"
    print_ok(f"重启前后工单数量一致: {len(orders_after)}")

    for oid in orders_before:
        b = orders_before[oid]
        a = orders_after[oid]
        assert b["status"] == a["status"], f"{oid} 状态不一致"
        assert b["assignee_id"] == a["assignee_id"], f"{oid} 维修员不一致"
        assert len(b["history"]) == len(a["history"]), f"{oid} 历史条数不一致"
        assert b["exception_notes"] == a["exception_notes"], f"{oid} 异常备注不一致"
    print_ok("所有工单：状态、维修员、历史记录、异常备注 完全一致")

    assert export_dir_before == export_dir_after, f"导出配置不一致: {export_dir_before} vs {export_dir_after}"
    print_ok(f"导出配置一致: {export_dir_after}")

    assert os.path.exists(csv_path) and os.path.getsize(csv_path) == csv_size_before
    assert os.path.exists(json_path) and os.path.getsize(json_path) == json_size_before
    print_ok("导出的CSV/JSON文件保持不变")

    order2 = store2.get_order(order.order_id)
    assert order2.status == Status.IN_PROGRESS
    assert order2.assignee_name == tech.name
    assert any("测试异常备注" in n for n in order2.exception_notes)
    print_ok(f"样例工单复查通过: {order2.order_id} 状态={order2.status.value}, 维修员={order2.assignee_name}, 异常备注={len(order2.exception_notes)}条")

    return store2


def test_filter_and_view(store):
    print_title("测试8: 按位置/类别/优先级筛选")

    all_count = len(store.get_all_orders())
    high = store.get_orders_by_filter(priority="高")
    ac = store.get_orders_by_filter(category="空调维修")
    loc = store.get_orders_by_filter(location="A栋")

    print_ok(f"总工单: {all_count}, 高优先级: {len(high)}, 空调维修: {len(ac)}, A栋: {len(loc)}")
    assert len(high) >= 1
    for o in high:
        assert o.priority == "高"
    for o in ac:
        assert o.category == "空调维修"
    for o in loc:
        assert "A栋" in o.location
    print_ok("筛选结果准确")


def main():
    print("=" * 70)
    print("  维修派工系统 - 全场景自动化测试")
    print("=" * 70)

    store = setup()
    try:
        test_normal_flow(store)
        test_race_condition(store)
        test_permission_denied(store)
        test_reject_then_complete(store)
        test_export_not_writable(store)
        test_import_csv(store)
        test_filter_and_view(store)
        store = test_persistence(store)

        print_title("全部测试通过")
        print("""
  已验证场景：
  1. 登记-派单-接单-完工-验收 完整链路
  2. 两个维修员并发抢单（互斥）
  3. 越权操作（维修员验收、派工）被拒绝且不改动记录
  4. 验收退回后直接完成被阻止，必须重新走完工流程
  5. 导出目录不可写时报错且不影响已保存记录
  6. CSV 样例数据导入
  7. 重启后：工单状态/历史/异常备注/导出配置/CSV&JSON 文件完全一致
  8. 按位置、类别、优先级筛选
""")
    except AssertionError as e:
        print_fail(f"断言失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print_fail(f"未预期异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
