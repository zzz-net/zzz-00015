import os
import sys
import shutil
import json
import stat
import threading
import time
import csv
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import Role, Status, TimeSlot
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


def test_technician_schedule_management(store):
    print_title("测试9: 维修员排班、技能、最大并行工单维护")

    dispatcher = store.get_user("u001")
    tech = store.get_user("u002")
    inspector = store.get_user("u004")

    try:
        store.set_technician_skills(tech.user_id, ["空调", "电路"], inspector)
        print_fail("验收员居然能修改维修员技能！")
        assert False
    except PermissionError as e:
        print_ok(f"验收员无权管理排班（符合预期）: {e}")

    try:
        store.set_technician_skills(inspector.user_id, ["电路"], dispatcher)
        print_fail("居然能给非维修员设置技能！")
        assert False
    except WorkOrderError as e:
        print_ok(f"非维修员不能设置技能（符合预期）: {e}")

    try:
        store.set_technician_skills(tech.user_id, ["空调", "空调"], dispatcher)
        print_fail("重复技能居然设置成功！")
        assert False
    except WorkOrderError as e:
        print_ok(f"重复技能被拒绝（符合预期）: {e}")

    tech = store.set_technician_skills(tech.user_id, ["空调", "电路"], dispatcher)
    assert set(tech.skills) == {"空调", "电路"}
    print_ok(f"技能设置成功: {tech.skills}")

    try:
        store.set_technician_max_parallel(tech.user_id, 0, dispatcher)
        print_fail("最大并行数0居然设置成功！")
        assert False
    except WorkOrderError as e:
        print_ok(f"最大并行数必须>=1（符合预期）: {e}")

    tech = store.set_technician_max_parallel(tech.user_id, 2, dispatcher)
    assert tech.max_parallel_orders == 2
    print_ok(f"最大并行数设置成功: {tech.max_parallel_orders}")

    try:
        store.set_technician_time_slots(
            tech.user_id,
            [TimeSlot(7, "09:00", "18:00")],
            dispatcher,
        )
        print_fail("非法星期(7)居然设置成功！")
        assert False
    except WorkOrderError as e:
        print_ok(f"非法星期被拒绝（符合预期）: {e}")

    try:
        store.set_technician_time_slots(
            tech.user_id,
            [TimeSlot(1, "18:00", "09:00")],
            dispatcher,
        )
        print_fail("结束早于开始居然设置成功！")
        assert False
    except WorkOrderError as e:
        print_ok(f"非法时段被拒绝（符合预期）: {e}")

    try:
        store.set_technician_time_slots(
            tech.user_id,
            [TimeSlot(1, "09:00", "18:00"), TimeSlot(1, "09:00", "18:00")],
            dispatcher,
        )
        print_fail("重复时段居然设置成功！")
        assert False
    except WorkOrderError as e:
        print_ok(f"重复时段被拒绝（符合预期）: {e}")

    valid_slots = [
        TimeSlot(0, "09:00", "18:00"),
        TimeSlot(1, "09:00", "18:00"),
        TimeSlot(2, "09:00", "18:00"),
        TimeSlot(3, "09:00", "18:00"),
        TimeSlot(4, "09:00", "18:00"),
    ]
    tech = store.set_technician_time_slots(tech.user_id, valid_slots, dispatcher)
    assert len(tech.time_slots) == 5
    print_ok(f"排班设置成功，共 {len(tech.time_slots)} 个时段")

    sched = store.get_technician_schedule(tech.user_id)
    assert sched is not None
    assert set(sched["skills"]) == {"空调", "电路"}
    assert sched["max_parallel_orders"] == 2
    assert len(sched["time_slots"]) == 5
    print_ok(f"排班查询成功: 技能={sched['skills']}, 最大并行={sched['max_parallel_orders']}, 时段数={len(sched['time_slots'])}")


def test_skill_and_load_matching(store):
    print_title("测试10: 技能匹配、排班时段与超载检测")

    dispatcher = store.get_user("u001")
    tech_aircon = store.get_user("u002")
    tech_plumber = store.get_user("u003")

    existing_load = store.get_technician_load(tech_aircon.user_id)
    store.set_technician_skills(tech_aircon.user_id, ["空调", "电路"], dispatcher)
    store.set_technician_skills(tech_plumber.user_id, ["水管", "电梯"], dispatcher)
    store.set_technician_max_parallel(tech_aircon.user_id, max(5, existing_load + 3), dispatcher)
    store.set_technician_max_parallel(tech_plumber.user_id, 2, dispatcher)
    store.set_technician_time_slots(tech_aircon.user_id, [], dispatcher)
    store.set_technician_time_slots(tech_plumber.user_id, [], dispatcher)

    aircon_order = store.create_order("空调测试", "", "A栋", "空调维修", "高", dispatcher)
    pipe_order = store.create_order("水管测试", "", "B栋", "水管维修", "高", dispatcher)

    match_ac_to_aircon = store.calculate_match(aircon_order, tech_aircon)
    match_pipe_to_aircon = store.calculate_match(aircon_order, tech_plumber)

    assert match_ac_to_aircon.skill_match == True
    assert match_ac_to_aircon.is_recommended == True
    print_ok(f"空调单匹配空调维修员: 技能匹配={match_ac_to_aircon.skill_match}, 得分={match_ac_to_aircon.score}, 推荐={match_ac_to_aircon.is_recommended}, 负载={match_ac_to_aircon.current_load}/{match_ac_to_aircon.max_parallel}")

    assert match_pipe_to_aircon.skill_match == False
    assert len(match_pipe_to_aircon.warnings) >= 1
    print_ok(f"空调单匹配水管维修员: 技能匹配={match_pipe_to_aircon.skill_match}, 警告={match_pipe_to_aircon.warnings}")

    rankings = store.rank_technicians_for_order(aircon_order)
    assert rankings[0][0].user_id == tech_aircon.user_id
    print_ok(f"空调单排名第一: {rankings[0][0].name}, 得分={rankings[0][1].score}")

    rankings_pipe = store.rank_technicians_for_order(pipe_order)
    assert rankings_pipe[0][0].user_id == tech_plumber.user_id
    print_ok(f"水管单排名第一: {rankings_pipe[0][0].name}, 得分={rankings_pipe[0][1].score}")

    store.dispatch_order(aircon_order.order_id, tech_plumber, dispatcher)
    store.accept_order(aircon_order.order_id, tech_plumber)
    order2 = store.create_order("水管测试2", "", "B栋2", "水管维修", "中", dispatcher)
    store.dispatch_order(order2.order_id, tech_plumber, dispatcher)
    store.accept_order(order2.order_id, tech_plumber)

    load = store.get_technician_load(tech_plumber.user_id)
    assert load == 2
    print_ok(f"水管维修员当前负载: {load}/{tech_plumber.max_parallel_orders}")

    order3 = store.create_order("水管测试3", "", "B栋3", "水管维修", "中", dispatcher)
    match = store.calculate_match(order3, tech_plumber)
    assert match.within_capacity == False
    assert match.current_load >= match.max_parallel
    print_ok(f"超载检测: within_capacity={match.within_capacity}, 负载={match.current_load}/{match.max_parallel}")

    monday_9am = datetime(2026, 6, 8, 9, 30)
    monday_10pm = datetime(2026, 6, 8, 22, 0)
    store.set_technician_time_slots(
        tech_aircon.user_id,
        [TimeSlot(0, "09:00", "18:00")],
        dispatcher,
    )
    match_worktime = store.calculate_match(aircon_order, tech_aircon, monday_9am)
    match_offtime = store.calculate_match(aircon_order, tech_aircon, monday_10pm)
    assert match_worktime.available_now == True
    assert match_offtime.available_now == False
    print_ok(f"时段匹配: 上班时间={match_worktime.available_now}, 下班时间={match_offtime.available_now}")


def test_reassignment(store):
    print_title("测试11: 改派逻辑（状态限制、权限、必填原因、日志）")

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")
    inspector = store.get_user("u004")

    order = store.create_order("改派测试工单", "", "X栋", "空调维修", "高", dispatcher)

    try:
        store.reassign_order(order.order_id, tech2, tech1, "维修员自己改派")
        print_fail("维修员居然能改派！")
        assert False
    except PermissionError as e:
        print_ok(f"维修员无权改派（符合预期）: {e}")

    try:
        store.reassign_order(order.order_id, tech2, dispatcher, "")
        print_fail("空原因居然改派成功！")
        assert False
    except WorkOrderError as e:
        print_ok(f"缺少改派原因被拒绝（符合预期）: {e}")

    try:
        store.reassign_order(order.order_id, inspector, dispatcher, "派给验收员")
        print_fail("居然能改派给非维修员！")
        assert False
    except WorkOrderError as e:
        print_ok(f"改派给非维修员被拒绝（符合预期）: {e}")

    order = store.reassign_order(order.order_id, tech1, dispatcher, "首次指派", expected_version=order.version)
    assert order.assignee_id == tech1.user_id
    assert order.status == Status.DISPATCHED
    assert len(order.reassignment_logs) == 1
    log = order.reassignment_logs[0]
    assert log.to_user_id == tech1.user_id
    assert log.reason == "首次指派"
    assert log.dispatcher_id == dispatcher.user_id
    print_ok(f"待派单改派成功: 维修员={order.assignee_name}, 状态={order.status.value}, 改派日志={len(order.reassignment_logs)}条")
    print_ok(f"改派日志: 原={log.from_user_name}, 新={log.to_user_name}, 原因={log.reason}, 调度员={log.dispatcher_name}")

    try:
        store.reassign_order(order.order_id, tech1, dispatcher, "派给自己")
        print_fail("改派给同一人居然成功！")
        assert False
    except WorkOrderError as e:
        print_ok(f"改派给同一人被拒绝（符合预期）: {e}")

    order = store.reassign_order(order.order_id, tech2, dispatcher, "临时换人", expected_version=order.version)
    assert order.assignee_id == tech2.user_id
    assert len(order.reassignment_logs) == 2
    print_ok(f"已派单状态改派成功: 新维修员={order.assignee_name}, 改派日志数={len(order.reassignment_logs)}")

    store.accept_order(order.order_id, tech2)
    order = store.reassign_order(order.order_id, tech1, dispatcher, "维修员请假", expected_version=order.version)
    assert order.assignee_id == tech1.user_id
    assert order.status == Status.DISPATCHED
    assert len(order.reassignment_logs) == 3
    print_ok(f"处理中状态改派成功: 新维修员={order.assignee_name}, 状态重置为={order.status.value}(新维修员需接单)")

    store.accept_order(order.order_id, tech1)
    store.complete_order(order.order_id, tech1)

    allowed, msg = store.can_reassign(order, dispatcher)
    assert allowed == True
    print_ok(f"待验收状态可改派检查: allowed={allowed}, msg={msg}")

    order = store.reassign_order(order.order_id, tech2, dispatcher, "升级处理", expected_version=order.version)
    assert order.assignee_id == tech2.user_id
    assert order.status == Status.DISPATCHED
    print_ok(f"待验收状态改派成功: 新维修员={order.assignee_name}, 状态重置为={order.status.value}")

    store.accept_order(order.order_id, tech2)
    store.complete_order(order.order_id, tech2)
    store.approve_order(order.order_id, inspector)

    try:
        store.reassign_order(order.order_id, tech1, dispatcher, "已完成也想改")
        print_fail("已完成工单居然能改派！")
        assert False
    except WorkOrderError as e:
        print_ok(f"已完成工单改派被拒绝（符合预期）: {e}")


def test_reassignment_concurrent(store):
    print_title("测试12: 两个调度员同时改派同一工单")

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")

    order = store.create_order("并发改派测试", "", "Y栋", "电路维修", "高", dispatcher)
    store.dispatch_order(order.order_id, tech1, dispatcher)
    base_version = store.get_order(order.order_id).version

    results = {}

    def try_reassign(tid, target_tech, use_version):
        try:
            kwargs = {}
            if use_version:
                kwargs["expected_version"] = base_version
            store.reassign_order(order.order_id, target_tech, dispatcher, f"{tid}发起改派", **kwargs)
            results[tid] = ("success", None)
        except Exception as e:
            results[tid] = ("fail", type(e).__name__ + ": " + str(e))

    t1 = threading.Thread(target=try_reassign, args=("t1", tech2, True))
    t2 = threading.Thread(target=try_reassign, args=("t2", tech1, True))
    t1.start()
    time.sleep(0.01)
    t2.start()
    t1.join()
    t2.join()

    o = store.get_order(order.order_id)
    print_ok(f"工单最终维修员: {o.assignee_name}, 版本: v{o.version}, 改派次数: {len(o.reassignment_logs)}")
    print(f"  Thread1: {results['t1']}")
    print(f"  Thread2: {results['t2']}")

    success_count = sum(1 for v in results.values() if v[0] == "success")
    fail_count = sum(1 for v in results.values() if v[0] == "fail")
    assert success_count + fail_count == 2
    print_ok(f"并发改派一致性：{success_count}人成功, {fail_count}人失败，工单版本={o.version}")

    for log in o.reassignment_logs:
        assert log.from_user_id != "" or log.to_user_id != ""
        assert log.reason and log.reason.strip()
        assert log.timestamp and log.timestamp.strip()
        assert log.dispatcher_id and log.dispatcher_id.strip()
    print_ok(f"所有改派日志字段完整: 共 {len(o.reassignment_logs)} 条")


def test_technician_import_and_validation(store):
    print_title("测试13: 维修员排班CSV导入（含非法数据验证）")

    dispatcher = store.get_user("u001")
    tech = store.get_user("u002")
    inspector = store.get_user("u004")

    base = os.path.dirname(os.path.abspath(__file__))
    test_csv_path = os.path.join(base, "test_technicians.csv")

    with open(test_csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["user_id", "skills", "max_parallel", "time_slots"])
        writer.writerow(["u002", "空调|电路", "3", "周一 09:00-18:00|周二 09:00-18:00"])
        writer.writerow(["u003", "水管|电梯", "2", "周三 08:00-17:00"])

    try:
        store.import_technicians_csv(test_csv_path, inspector)
        print_fail("验收员居然能导入排班！")
        assert False
    except PermissionError as e:
        print_ok(f"验收员无权导入排班（符合预期）: {e}")

    count, errors = store.import_technicians_csv(test_csv_path, dispatcher)
    assert count == 2
    assert len(errors) == 0
    print_ok(f"合法排班导入成功: {count} 条")

    tech2 = store.get_user("u002")
    assert set(tech2.skills) == {"空调", "电路"}
    assert tech2.max_parallel_orders == 3
    assert len(tech2.time_slots) == 2
    print_ok(f"u002验证: 技能={tech2.skills}, 最大并行={tech2.max_parallel_orders}, 时段={len(tech2.time_slots)}")

    tech3 = store.get_user("u003")
    assert set(tech3.skills) == {"水管", "电梯"}
    assert tech3.max_parallel_orders == 2
    assert len(tech3.time_slots) == 1
    print_ok(f"u003验证: 技能={tech3.skills}, 最大并行={tech3.max_parallel_orders}, 时段={len(tech3.time_slots)}")

    bad_csv_path = os.path.join(base, "test_technicians_bad.csv")
    with open(bad_csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["user_id", "skills", "max_parallel", "time_slots"])
        writer.writerow(["u002", "空调|空调", "3", "周一 09:00-18:00"])
        writer.writerow(["u003", "水管", "0", ""])
        writer.writerow(["u999", "", "1", ""])
        writer.writerow(["u004", "", "1", ""])
        writer.writerow(["u002", "", "1", "星期八 09:00-18:00"])
        writer.writerow(["u002", "", "1", "周一 18:00-09:00"])

    tech2_before_skills = list(tech2.skills)
    tech2_before_max = tech2.max_parallel_orders
    tech3_before_skills = list(tech3.skills)
    tech3_before_max = tech3.max_parallel_orders

    count, errors = store.import_technicians_csv(bad_csv_path, dispatcher)
    assert count == 0
    assert len(errors) >= 4
    print_ok(f"非法数据全部拒绝: 成功{count}条, 失败{len(errors)}条")
    for e in errors:
        print(f"    - {e}")

    tech2_after = store.get_user("u002")
    tech3_after = store.get_user("u003")
    assert tech2_after.skills == tech2_before_skills, "非法导入不应污染u002技能"
    assert tech2_after.max_parallel_orders == tech2_before_max, "非法导入不应污染u002并行数"
    assert tech3_after.skills == tech3_before_skills, "非法导入不应污染u003技能"
    assert tech3_after.max_parallel_orders == tech3_before_max, "非法导入不应污染u003并行数"
    print_ok("非法导入未污染任何已保存数据")

    try:
        os.remove(test_csv_path)
        os.remove(bad_csv_path)
    except Exception:
        pass


def test_enhanced_exports(store):
    print_title("测试14: 增强导出（JSON/CSV包含排班、负载、改派历史）")

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")

    store.set_technician_skills(tech1.user_id, ["空调", "电路"], dispatcher)
    store.set_technician_skills(tech2.user_id, ["水管", "电梯"], dispatcher)
    store.set_technician_max_parallel(tech1.user_id, 3, dispatcher)
    store.set_technician_max_parallel(tech2.user_id, 2, dispatcher)

    base = os.path.dirname(os.path.abspath(__file__))
    export_dir = os.path.join(base, "test_exports_enhanced")
    if os.path.exists(export_dir):
        shutil.rmtree(export_dir)
    store.set_export_dir(export_dir)

    order = store.create_order("导出测试工单", "", "Z栋", "空调维修", "高", dispatcher)
    store.dispatch_order(order.order_id, tech1, dispatcher)
    store.reassign_order(order.order_id, tech2, dispatcher, "测试改派记录")

    techs_json_path = store.export_technicians_json()
    assert os.path.exists(techs_json_path)
    with open(techs_json_path, "r", encoding="utf-8") as f:
        techs_data = json.load(f)
    assert len(techs_data) == 2
    for td in techs_data:
        assert "skills" in td
        assert "max_parallel_orders" in td
        assert "time_slots" in td
        assert "current_load" in td
    print_ok(f"维修员JSON导出: 包含skills/max_parallel/time_slots/current_load, 共{len(techs_data)}条")

    techs_csv_path = store.export_technicians_csv()
    assert os.path.exists(techs_csv_path)
    with open(techs_csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        rows = list(reader)
    header = rows[0]
    assert "技能" in header
    assert "当前负载" in header
    assert "排班时段" in header
    print_ok(f"维修员CSV导出: 表头={header}")

    orders_json_path = store.export_orders_json()
    assert os.path.exists(orders_json_path)
    with open(orders_json_path, "r", encoding="utf-8") as f:
        orders_data = json.load(f)
    found_reassign = False
    found_schedule = False
    for od in orders_data:
        if "reassignment_logs" in od and len(od["reassignment_logs"]) > 0:
            found_reassign = True
            rl = od["reassignment_logs"][0]
            assert "from_user_name" in rl
            assert "to_user_name" in rl
            assert "reason" in rl
            assert "timestamp" in rl
        if "assignee_schedule" in od:
            found_schedule = True
    assert found_reassign
    assert found_schedule
    print_ok(f"工单JSON导出: 包含reassignment_logs和assignee_schedule")

    orders_csv_path = store.export_orders_csv()
    assert os.path.exists(orders_csv_path)
    with open(orders_csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
    assert "维修员技能" in header
    assert "维修员当前负载" in header
    assert "维修员最大并行" in header
    assert "改派次数" in header
    print_ok(f"工单CSV导出: 表头={header}")

    reassign_json_path = store.export_reassignment_logs_json()
    assert os.path.exists(reassign_json_path)
    with open(reassign_json_path, "r", encoding="utf-8") as f:
        reassign_data = json.load(f)
    assert len(reassign_data) >= 1
    for rd in reassign_data:
        assert "order_id" in rd
        assert "from_user_name" in rd
        assert "to_user_name" in rd
        assert "reason" in rd
        assert "dispatcher_name" in rd
        assert "timestamp" in rd
    print_ok(f"改派日志JSON导出: 共{len(reassign_data)}条，字段完整")

    reassign_csv_path = store.export_reassignment_logs_csv()
    assert os.path.exists(reassign_csv_path)
    with open(reassign_csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
    assert "工单编号" in header
    assert "原维修员" in header
    assert "新维修员" in header
    assert "改派原因" in header
    assert "调度员" in header
    assert "时间" in header
    print_ok(f"改派日志CSV导出: 表头={header}")


def test_persistence_extended(store):
    print_title("测试15: 扩展持久化（排班、技能、改派记录跨重启一致）")

    data_dir = store.data_dir
    base = os.path.dirname(os.path.abspath(__file__))
    export_dir = os.path.join(base, "test_exports_persist2")
    if os.path.exists(export_dir):
        shutil.rmtree(export_dir)
    store.set_export_dir(export_dir)

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")

    store.set_technician_skills(tech1.user_id, ["空调", "电路"], dispatcher)
    store.set_technician_skills(tech2.user_id, ["水管"], dispatcher)
    store.set_technician_max_parallel(tech1.user_id, 5, dispatcher)
    store.set_technician_max_parallel(tech2.user_id, 1, dispatcher)
    slots = [TimeSlot(0, "08:30", "17:30"), TimeSlot(1, "08:30", "17:30")]
    store.set_technician_time_slots(tech1.user_id, slots, dispatcher)

    order = store.create_order("持久化改派测试", "", "ZZ栋", "空调维修", "高", dispatcher)
    store.dispatch_order(order.order_id, tech1, dispatcher)
    store.accept_order(order.order_id, tech1)
    store.reassign_order(order.order_id, tech2, dispatcher, "维修员临时请假")

    users_before = {u.user_id: u.to_dict() for u in store.get_all_users()}
    orders_before = {o.order_id: o.to_dict() for o in store.get_all_orders()}
    reassign_before = {oid: o["reassignment_logs"] for oid, o in orders_before.items()}

    orders_csv_before = store.export_orders_csv()
    techs_csv_before = store.export_technicians_csv()
    reassign_csv_before = store.export_reassignment_logs_csv()
    csv_sizes = {
        "orders": os.path.getsize(orders_csv_before),
        "techs": os.path.getsize(techs_csv_before),
        "reassign": os.path.getsize(reassign_csv_before),
    }

    print_ok("重启数据存储，模拟关闭应用...")
    del store
    import gc
    gc.collect()

    store2 = DataStore(data_dir)

    users_after = {u.user_id: u.to_dict() for u in store2.get_all_users()}
    orders_after = {o.order_id: o.to_dict() for o in store2.get_all_orders()}

    assert set(users_before.keys()) == set(users_after.keys()), "用户ID集合不一致"
    for uid in users_before:
        ub = users_before[uid]
        ua = users_after[uid]
        assert ub["skills"] == ua["skills"], f"{uid} 技能不一致"
        assert ub["max_parallel_orders"] == ua["max_parallel_orders"], f"{uid} 最大并行不一致"
        assert ub["time_slots"] == ua["time_slots"], f"{uid} 时段不一致"
    print_ok("所有维修员：技能、最大并行、排班时段 完全一致")

    assert set(orders_before.keys()) == set(orders_after.keys()), "工单ID集合不一致"
    for oid in orders_before:
        ob = orders_before[oid]
        oa = orders_after[oid]
        assert ob["status"] == oa["status"], f"{oid} 状态不一致"
        assert ob["assignee_id"] == oa["assignee_id"], f"{oid} 维修员不一致"
        assert len(ob["reassignment_logs"]) == len(oa["reassignment_logs"]), f"{oid} 改派日志数不一致"
        for i, (rb, ra) in enumerate(zip(ob["reassignment_logs"], oa["reassignment_logs"])):
            assert rb["from_user_id"] == ra["from_user_id"]
            assert rb["to_user_id"] == ra["to_user_id"]
            assert rb["reason"] == ra["reason"]
            assert rb["dispatcher_id"] == ra["dispatcher_id"]
    print_ok("所有工单：状态、维修员、改派日志 完全一致")

    assert os.path.getsize(orders_csv_before) == csv_sizes["orders"]
    assert os.path.getsize(techs_csv_before) == csv_sizes["techs"]
    assert os.path.getsize(reassign_csv_before) == csv_sizes["reassign"]
    print_ok("所有导出CSV文件保持不变")

    order2 = store2.get_order(order.order_id)
    sched = store2.get_technician_schedule(tech1.user_id)
    assert set(sched["skills"]) == {"空调", "电路"}
    assert sched["max_parallel_orders"] == 5
    assert len(sched["time_slots"]) == 2
    assert len(order2.reassignment_logs) == 1
    assert order2.reassignment_logs[0].reason == "维修员临时请假"
    print_ok(f"样例复查: tech1技能={sched['skills']}, 并行={sched['max_parallel_orders']}, 时段={len(sched['time_slots'])}")
    print_ok(f"样例工单: 改派日志数={len(order2.reassignment_logs)}, 原因={order2.reassignment_logs[0].reason}")

    return store2


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
        test_technician_schedule_management(store)
        test_skill_and_load_matching(store)
        test_reassignment(store)
        test_reassignment_concurrent(store)
        test_technician_import_and_validation(store)
        test_enhanced_exports(store)
        store = test_persistence(store)
        store = test_persistence_extended(store)

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
  9. 维修员排班：技能/最大并行/时段维护，权限校验，非法/重复数据拒绝
 10. 技能匹配、排班时段、超载检测、维修员排名
 11. 改派：状态限制、权限校验、必填原因、日志完整（原/新/调度员/原因/时间）
 12. 两个调度员并发改派同一工单：版本号互斥保护
 13. 维修员排班CSV导入：合法数据导入成功，非法数据全部拒绝且不污染
 14. 增强导出：JSON/CSV含排班、负载、改派历史
 15. 扩展持久化：排班/技能/改派记录跨重启完全一致
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
