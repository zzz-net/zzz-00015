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


def test_gui_startup_and_tabs():
    print_title("测试16: GUI 启动与页面切换回归（导入导出 TclError 修复）")

    import tkinter as tk
    from tkinter import messagebox

    gui_data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_test_data")
    if os.path.exists(gui_data_dir):
        shutil.rmtree(gui_data_dir)
    gui_export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_test_exports")
    if os.path.exists(gui_export_dir):
        shutil.rmtree(gui_export_dir)

    captured = {"showerror": [], "showinfo": [], "showwarning": []}

    def fake_showerror(title, msg):
        captured["showerror"].append((title, msg))

    def fake_showinfo(title, msg):
        captured["showinfo"].append((title, msg))

    def fake_showwarning(title, msg):
        captured["showwarning"].append((title, msg))

    orig_showerror = messagebox.showerror
    orig_showinfo = messagebox.showinfo
    orig_showwarning = messagebox.showwarning
    messagebox.showerror = fake_showerror
    messagebox.showinfo = fake_showinfo
    messagebox.showwarning = fake_showwarning

    root = None
    try:
        store = DataStore(gui_data_dir)
        store.set_export_dir(gui_export_dir)
        dispatcher = store.get_user("u001")
        assert dispatcher.role == Role.DISPATCHER

        root = tk.Tk()
        root.withdraw()
        root.update()

        from main import MaintenanceApp
        app = MaintenanceApp.__new__(MaintenanceApp)
        app.root = root
        app.store = store
        app.current_user = dispatcher
        app._configure_styles()

        try:
            app._build_main_ui()
        except tk.TclError as e:
            print_fail(f"GUI 启动时抛出 TclError: {e}")
            raise

        print_ok(f"调度员主界面构建成功，无 TclError")
        root.update()

        tab_count = app.notebook.index("end")
        assert tab_count == 5, f"调度员应有 5 个 Tab，实际 {tab_count}"
        print_ok(f"调度员 Tab 数量正确: {tab_count} 个")

        expected_tabs = ["工单列表", "历史记录", "调度派工", "排班管理", "导入导出"]
        actual_tabs = [app.notebook.tab(i, "text") for i in range(tab_count)]
        for t in expected_tabs:
            assert t in actual_tabs, f"缺少 Tab: {t}"
        print_ok(f"所有预期 Tab 存在: {actual_tabs}")

        for i, tab_name in enumerate(actual_tabs):
            app.notebook.select(i)
            root.update()
            print_ok(f"切换 Tab 成功: {tab_name}")

        app.notebook.select(4)
        root.update()
        assert hasattr(app, "export_log"), "导入导出页缺少 export_log 控件"
        assert hasattr(app, "export_dir_label"), "导入导出页缺少 export_dir_label 控件"
        print_ok("导入导出页控件创建正常: export_log、export_dir_label 存在")

        app._on_export_orders("json")
        root.update()
        assert len(captured["showinfo"]) >= 1, "导出 JSON 未弹出成功提示"
        json_files = [f for f in os.listdir(gui_export_dir) if f.endswith(".json") and "work_orders" in f]
        assert len(json_files) >= 1, "未找到导出的工单 JSON 文件"
        print_ok(f"工单 JSON 导出成功: {json_files[-1]}")

        app._on_export_techs("csv")
        root.update()
        csv_files = [f for f in os.listdir(gui_export_dir) if f.endswith(".csv") and "technicians" in f]
        assert len(csv_files) >= 1, "未找到导出的维修员 CSV 文件"
        print_ok(f"维修员 CSV 导出成功: {csv_files[-1]}")

        bad_csv = os.path.join(gui_data_dir, "bad_techs.csv")
        with open(bad_csv, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["user_id", "skills", "max_parallel_orders", "time_slots"])
            writer.writerow(["u002", "空调|空调", "2", "周一 18:00-09:00"])
        before_skills = list(store.get_user("u002").skills)
        before_max = store.get_user("u002").max_parallel_orders
        before_slots = [s.to_dict() for s in store.get_user("u002").time_slots]
        captured["showerror"].clear()
        captured["showinfo"].clear()

        import main as main_mod
        orig_askopen = main_mod.filedialog.askopenfilename
        try:
            main_mod.filedialog.askopenfilename = lambda **kw: bad_csv
            app._on_import_techs()
            root.update()
        finally:
            main_mod.filedialog.askopenfilename = orig_askopen

        after_skills = list(store.get_user("u002").skills)
        after_max = store.get_user("u002").max_parallel_orders
        after_slots = [s.to_dict() for s in store.get_user("u002").time_slots]
        assert before_skills == after_skills, f"非法导入污染了技能: {before_skills} -> {after_skills}"
        assert before_max == after_max, f"非法导入污染了最大并行数: {before_max} -> {after_max}"
        assert before_slots == after_slots, f"非法导入污染了时段: {before_slots} -> {after_slots}"
        assert len(captured["showinfo"]) >= 1 or len(captured["showerror"]) >= 1
        last_msg = captured["showinfo"][-1][1] if captured["showinfo"] else captured["showerror"][-1][1]
        assert "失败" in last_msg or "0 条" in last_msg, f"非法导入应报告失败，实际消息: {last_msg}"
        print_ok(f"非法排班 CSV 导入被正确拒绝，未污染数据。提示: {last_msg[:60]}")

        app.notebook.select(3)
        root.update()
        assert hasattr(app, "schedule_tech_combo"), "排班页缺少 schedule_tech_combo"
        assert hasattr(app, "skills_listbox"), "排班页缺少 skills_listbox"
        assert hasattr(app, "slots_listbox"), "排班页缺少 slots_listbox"
        print_ok("排班管理页控件创建正常")

        app.notebook.select(2)
        root.update()
        assert hasattr(app, "dispatch_order_tree"), "调度派工页缺少 dispatch_order_tree"
        assert hasattr(app, "match_tree"), "调度派工页缺少 match_tree"
        print_ok("调度派工页控件创建正常（匹配度表格存在）")

        app.notebook.select(0)
        root.update()
        assert hasattr(app, "orders_tree"), "工单列表页缺少 orders_tree"
        print_ok("工单列表页控件创建正常")

        app.notebook.select(1)
        root.update()
        assert hasattr(app, "history_tree"), "历史记录页缺少 history_tree"
        assert hasattr(app, "reassign_tree"), "历史记录页缺少 reassign_tree"
        print_ok("历史记录页控件创建正常（含改派记录表）")

        print_ok("GUI 全页面切换、导出、非法导入拒绝 全部验证通过，无 TclError")
        return store

    finally:
        messagebox.showerror = orig_showerror
        messagebox.showinfo = orig_showinfo
        messagebox.showwarning = orig_showwarning
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        if os.path.exists(gui_data_dir):
            shutil.rmtree(gui_data_dir)
        if os.path.exists(gui_export_dir):
            shutil.rmtree(gui_export_dir)


def test_reassignment_drafts_basic(store):
    print_title("测试17: 改派草稿 - 保存、读取、删除、跨重启、改派成功自动清理")

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")

    order = store.create_order("草稿测试工单", "", "DRAFT栋", "空调维修", "高", dispatcher)
    store.dispatch_order(order.order_id, tech1, dispatcher)
    version_before = store.get_order(order.order_id).version

    draft = store.save_reassignment_draft(order.order_id, dispatcher, tech2, "测试草稿保存")
    assert draft.order_id == order.order_id
    assert draft.dispatcher_id == dispatcher.user_id
    assert draft.target_technician_id == tech2.user_id
    assert draft.reason == "测试草稿保存"
    assert draft.order_version == version_before
    assert draft.created_at and draft.created_at.strip()
    print_ok(f"草稿保存成功: order_id={draft.order_id}, target={tech2.name}, version=v{draft.order_version}")

    loaded = store.get_reassignment_draft(order.order_id, dispatcher)
    assert loaded is not None
    assert loaded.target_technician_id == tech2.user_id
    assert loaded.reason == "测试草稿保存"
    assert loaded.order_version == version_before
    print_ok(f"草稿读取成功: 目标维修员={tech2.name}, 原因={loaded.reason}")

    other_dispatcher_draft = store.get_reassignment_draft(order.order_id, store.get_user("u004"))
    assert other_dispatcher_draft is None, "其他用户不能读取不属于自己的草稿"
    print_ok("草稿按调度员隔离：其他调度员无法读取")

    data_dir = store.data_dir
    drafts_path = os.path.join(data_dir, "reassignment_drafts.json")
    assert os.path.exists(drafts_path), "草稿持久化文件不存在"
    with open(drafts_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    assert len(raw) >= 1
    saved_draft = [d for d in raw if d["order_id"] == order.order_id][0]
    assert saved_draft["target_technician_id"] == tech2.user_id
    print_ok(f"草稿持久化文件存在且内容正确: {drafts_path}")

    print_ok("重启数据存储，模拟关闭应用...")
    del store
    import gc
    gc.collect()
    store2 = DataStore(data_dir)

    dispatcher2 = store2.get_user("u001")
    tech2_2 = store2.get_user("u003")
    restored = store2.get_reassignment_draft(order.order_id, dispatcher2)
    assert restored is not None, "重启后草稿未恢复"
    assert restored.target_technician_id == tech2_2.user_id
    assert restored.reason == "测试草稿保存"
    assert restored.order_version == version_before
    print_ok(f"草稿跨重启恢复成功: 目标={tech2_2.name}, 原因={restored.reason}")

    order2 = store2.get_order(order.order_id)
    store2.reassign_order(order.order_id, tech2_2, dispatcher2, "正式改派", expected_version=order2.version)
    assert len(order2.reassignment_logs) >= 1
    after_reassign_log = store2.get_order(order.order_id).reassignment_logs[-1]
    assert after_reassign_log.to_user_id == tech2_2.user_id
    assert after_reassign_log.reason == "正式改派"
    print_ok(f"正式改派成功: 写入改派日志, 新维修员={after_reassign_log.to_user_name}")

    after_success_draft = store2.get_reassignment_draft(order.order_id, dispatcher2)
    assert after_success_draft is None, "改派成功后草稿未自动清理"
    print_ok("改派成功后草稿自动清理")

    order3 = store2.create_order("草稿删除测试", "", "DRAFT2栋", "电路维修", "中", dispatcher2)
    store2.dispatch_order(order3.order_id, tech2_2, dispatcher2)
    store2.save_reassignment_draft(order3.order_id, dispatcher2, store2.get_user("u002"), "待删除草稿")
    draft_before_del = store2.get_reassignment_draft(order3.order_id, dispatcher2)
    assert draft_before_del is not None

    deleted = store2.delete_reassignment_draft(order3.order_id, dispatcher2)
    assert deleted == True
    draft_after_del = store2.get_reassignment_draft(order3.order_id, dispatcher2)
    assert draft_after_del is None
    print_ok("草稿手动删除成功")

    delete_again = store2.delete_reassignment_draft(order3.order_id, dispatcher2)
    assert delete_again == False, "删除不存在的草稿应返回 False"
    print_ok("重复删除不存在的草稿返回 False，不报错")

    return store2


def test_reassignment_drafts_conflicts(store):
    print_title("测试18: 改派草稿 - 权限拒绝、版本冲突、状态变更场景")

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")
    inspector = store.get_user("u004")

    order = store.create_order("冲突测试工单", "", "CONFLICT栋", "空调维修", "高", dispatcher)
    store.dispatch_order(order.order_id, tech1, dispatcher)
    base_version = store.get_order(order.order_id).version

    store.save_reassignment_draft(order.order_id, dispatcher, tech2, "冲突场景草稿")
    assert store.get_reassignment_draft(order.order_id, dispatcher) is not None
    print_ok("草稿预保存成功，准备测试冲突场景")

    try:
        store.save_reassignment_draft(order.order_id, inspector, tech2, "验收员想存草稿")
        print_fail("验收员居然能保存改派草稿！")
        assert False
    except PermissionError as e:
        print_ok(f"验收员无权保存草稿（符合预期）: {e}")

    draft_after_perm_denied = store.get_reassignment_draft(order.order_id, dispatcher)
    assert draft_after_perm_denied is not None, "权限拒绝后草稿被误删"
    print_ok("权限拒绝后草稿保留")

    other_tech = store.get_user("u002")
    try:
        store.delete_reassignment_draft(order.order_id, other_tech)
        print_ok("非调度员用户无权删除草稿，delete 返回 False（符合预期）")
    except Exception as e:
        print_fail(f"非调度员删除草稿抛出异常: {e}")
        assert False

    order_obj = store.get_order(order.order_id)
    store.reassign_order(order.order_id, tech2, dispatcher, "他人抢先改派", expected_version=order_obj.version)
    order_after_other = store.get_order(order.order_id)
    assert order_after_other.version > base_version
    print_ok(f"模拟他人抢先改派: 版本从 v{base_version} 升级到 v{order_after_other.version}")

    draft_after_version_change = store.get_reassignment_draft(order.order_id, dispatcher)
    assert draft_after_version_change is None
    print_ok("他人成功改派后，目标工单的草稿已被自动清理")

    order2 = store.create_order("状态变更测试工单", "", "STATUS栋", "水管维修", "高", dispatcher)
    store.dispatch_order(order2.order_id, tech1, dispatcher)
    store.accept_order(order2.order_id, tech1)
    store.complete_order(order2.order_id, tech1)

    store.save_reassignment_draft(order2.order_id, dispatcher, tech2, "状态变更场景草稿")
    saved = store.get_reassignment_draft(order2.order_id, dispatcher)
    assert saved is not None
    assert saved.order_version == store.get_order(order2.order_id).version
    print_ok(f"待验收状态保存草稿成功: version=v{saved.order_version}")

    order2_obj = store.get_order(order2.order_id)
    store.approve_order(order2.order_id, inspector)
    final_order = store.get_order(order2.order_id)
    assert final_order.status == Status.COMPLETED
    print_ok("工单流转到已完成状态")

    draft_after_completed = store.get_reassignment_draft(order2.order_id, dispatcher)
    assert draft_after_completed is not None, "草稿应保留直到提交校验时才提示"
    print_ok("已完成状态的工单草稿仍然保留，提交时才给出提示")

    try:
        store.reassign_order(order2.order_id, tech1, dispatcher, "已完成工单尝试改派")
        print_fail("已完成工单居然能改派！")
        assert False
    except WorkOrderError as e:
        print_ok(f"已完成工单改派被拒绝（符合预期）: {e}")

    still_exists = store.get_reassignment_draft(order2.order_id, dispatcher)
    assert still_exists is not None, "改派失败后草稿被误删"
    print_ok("改派失败后草稿保留")

    try:
        store.save_reassignment_draft(order2.order_id, dispatcher, store.get_user("u004"), "派给验收员的草稿")
        print_fail("保存草稿给非维修员居然成功！")
        assert False
    except WorkOrderError as e:
        print_ok(f"草稿目标必须是维修员（符合预期）: {e}")

    fake_order_id = "WO_NONEXISTENT_12345"
    try:
        store.save_reassignment_draft(fake_order_id, dispatcher, tech1, "不存在工单的草稿")
        print_fail("不存在工单居然能保存草稿！")
        assert False
    except WorkOrderError as e:
        print_ok(f"不存在工单保存草稿被拒绝（符合预期）: {e}")

    data_dir = store.data_dir

    order_restart = store.create_order("重启草稿版本冲突", "", "RST栋", "水管维修", "高", dispatcher)
    store.dispatch_order(order_restart.order_id, tech1, dispatcher)
    order_restart_v1 = store.get_order(order_restart.order_id).version
    store.save_reassignment_draft(order_restart.order_id, dispatcher, tech2, "草稿保存时是v1")
    saved_v1_draft = store.get_reassignment_draft(order_restart.order_id, dispatcher)
    assert saved_v1_draft.order_version == order_restart_v1
    print_ok(f"保存草稿: 工单版本 v{order_restart_v1}")

    drafts_path = os.path.join(data_dir, "reassignment_drafts.json")
    with open(drafts_path, "r", encoding="utf-8") as f:
        raw_drafts_before = json.load(f)
    stale_draft = None
    for d in raw_drafts_before:
        if d["order_id"] == order_restart.order_id:
            stale_draft = d.copy()
            stale_draft["order_version"] = order_restart_v1
    assert stale_draft is not None
    stale_draft["target_technician_id"] = tech1.user_id
    stale_draft["reason"] = "手动回写的旧版本v1草稿"
    print_ok(f"提前备份草稿原始内容，旧版本号锁定为 v{order_restart_v1}")

    order_restart_obj = store.get_order(order_restart.order_id)
    store.reassign_order(order_restart.order_id, tech2, dispatcher, "他人抢先改派",
                          expected_version=order_restart_obj.version)
    order_restart_v2 = store.get_order(order_restart.order_id).version
    assert order_restart_v2 > order_restart_v1
    print_ok(f"模拟他人抢先改派: 工单版本从 v{order_restart_v1} 升级到 v{order_restart_v2}")

    other_drafts = [d for d in raw_drafts_before if d["order_id"] != order_restart.order_id]
    other_drafts.append(stale_draft)
    with open(drafts_path, "w", encoding="utf-8") as f:
        json.dump(other_drafts, f, ensure_ascii=False, indent=2)
    print_ok(f"手动模拟跨重启回写: 草稿版本保留在 v{order_restart_v1}，工单已升级到 v{order_restart_v2}")

    import gc as _gc
    del store
    _gc.collect()
    store_restarted = DataStore(data_dir)
    dispatcher_r = store_restarted.get_user("u001")
    tech1_r = store_restarted.get_user("u002")

    restored_stale = store_restarted.get_reassignment_draft(order_restart.order_id, dispatcher_r)
    assert restored_stale is not None, "跨重启后旧草稿未恢复"
    assert restored_stale.order_version == order_restart_v1, f"旧草稿版本不对: {restored_stale.order_version}"
    current_order_r = store_restarted.get_order(order_restart.order_id)
    assert current_order_r.version == order_restart_v2, f"当前工单版本不对: {current_order_r.version}"
    print_ok(f"跨重启恢复: 草稿v{restored_stale.order_version} vs 工单v{current_order_r.version}")

    logs_before = len(store_restarted.get_reassignment_logs(order_restart.order_id))
    try:
        store_restarted.reassign_order(order_restart.order_id, tech1_r, dispatcher_r,
                                       "基于旧草稿v1尝试再次改派",
                                       expected_version=restored_stale.order_version)
        print_fail("基于旧版本草稿的改派居然通过了版本校验！")
        assert False
    except ConcurrentOperationError as e:
        print_ok(f"DataStore层旧草稿版本冲突拦截成功（符合预期）: {e}")

    logs_after = len(store_restarted.get_reassignment_logs(order_restart.order_id))
    assert logs_after == logs_before, "版本冲突时不应该写入新的改派日志"
    print_ok(f"版本冲突时未写入改派日志: 日志数量保持 {logs_before} 条")

    draft_after_conflict = store_restarted.get_reassignment_draft(order_restart.order_id, dispatcher_r)
    assert draft_after_conflict is not None, "版本冲突时不应该清理草稿"
    assert draft_after_conflict.order_version == order_restart_v1
    print_ok("版本冲突时草稿保留，未被清理")

    order_after_conflict = store_restarted.get_order(order_restart.order_id)
    assert order_after_conflict.assignee_id == tech2.user_id, "版本冲突时工单不应被改动"
    assert order_after_conflict.version == order_restart_v2, "版本冲突时版本号不应变化"
    print_ok(f"版本冲突时工单数据未被覆盖: 维修员={order_after_conflict.assignee_name}, 版本=v{order_after_conflict.version}")


def test_gui_reassign_drafts():
    print_title("测试19: GUI 改派草稿功能回归（自动载入、清除、冲突保留草稿）")

    import tkinter as tk
    from tkinter import messagebox

    gui_data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_draft_test_data")
    if os.path.exists(gui_data_dir):
        shutil.rmtree(gui_data_dir)
    gui_export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_draft_test_exports")
    if os.path.exists(gui_export_dir):
        shutil.rmtree(gui_export_dir)

    captured = {"showerror": [], "showinfo": [], "showwarning": [], "askyesno": []}

    def fake_showerror(title, msg, **kw):
        captured["showerror"].append((title, msg))

    def fake_showinfo(title, msg, **kw):
        captured["showinfo"].append((title, msg))

    def fake_showwarning(title, msg, **kw):
        captured["showwarning"].append((title, msg))

    def fake_askyesno(title, msg, **kw):
        captured["askyesno"].append((title, msg))
        return True

    orig_showerror = messagebox.showerror
    orig_showinfo = messagebox.showinfo
    orig_showwarning = messagebox.showwarning
    orig_askyesno = messagebox.askyesno
    messagebox.showerror = fake_showerror
    messagebox.showinfo = fake_showinfo
    messagebox.showwarning = fake_showwarning
    messagebox.askyesno = fake_askyesno

    root = None
    try:
        store = DataStore(gui_data_dir)
        store.set_export_dir(gui_export_dir)
        dispatcher = store.get_user("u001")
        tech1 = store.get_user("u002")
        tech2 = store.get_user("u003")
        assert dispatcher.role == Role.DISPATCHER

        order = store.create_order("GUI草稿测试", "", "GUI栋", "空调维修", "高", dispatcher)
        store.dispatch_order(order.order_id, tech1, dispatcher)

        store.save_reassignment_draft(order.order_id, dispatcher, tech2, "GUI预存草稿理由")
        saved_draft = store.get_reassignment_draft(order.order_id, dispatcher)
        assert saved_draft is not None
        print_ok(f"预存草稿成功: 目标={tech2.name}, 原因=GUI预存草稿理由")

        root = tk.Tk()
        root.withdraw()
        root.update()

        from main import MaintenanceApp, ReassignDialog
        app = MaintenanceApp.__new__(MaintenanceApp)
        app.root = root
        app.store = store
        app.current_user = dispatcher
        app._configure_styles()

        fresh_order = store.get_order(order.order_id)
        dlg = ReassignDialog.__new__(ReassignDialog)
        dlg.store = store
        dlg.dispatcher = dispatcher
        dlg.order = fresh_order
        dlg.result = None
        dlg.parent = root
        ReassignDialog.__init__(dlg, root, store, dispatcher, fresh_order)
        root.update()

        assert hasattr(dlg, "draft_info_label"), "缺少 draft_info_label 控件"
        assert hasattr(dlg, "clear_draft_btn"), "缺少 clear_draft_btn 控件"
        print_ok("改派对话框草稿相关控件创建正常")

        selection = dlg.tree.selection()
        assert len(selection) == 1 and selection[0] == tech2.user_id, f"草稿未自动选中目标维修员, 实际={selection}"
        print_ok(f"草稿自动载入: 维修员树选中 {tech2.user_id} ({tech2.name})")

        loaded_reason = dlg.reason_text.get("1.0", tk.END).strip()
        assert loaded_reason == "GUI预存草稿理由", f"草稿原因未自动回填, 实际='{loaded_reason}'"
        print_ok(f"草稿原因自动回填: '{loaded_reason}'")

        draft_info_text = dlg.draft_info_label.cget("text")
        assert "已载入改派草稿" in draft_info_text, f"草稿信息提示内容不正确, 实际='{draft_info_text}'"
        draft_info_bg = dlg.draft_info_label.cget("bg")
        assert draft_info_bg == "#fff3cd", f"草稿提示条背景色不正确"
        print_ok("草稿载入提示条配置正确（内容和背景色）")

        clear_btn_text = dlg.clear_draft_btn.cget("text")
        assert clear_btn_text == "清除草稿", f"清除草稿按钮文字不正确"
        print_ok("清除草稿按钮配置正确")

        captured["showinfo"].clear()
        captured["askyesno"].clear()
        dlg._on_clear_draft()
        root.update()
        assert len(captured["askyesno"]) >= 1, "清除草稿未弹确认框"
        assert len(captured["showinfo"]) >= 1, "清除草稿未弹成功提示"
        print_ok("清除草稿: 确认框和成功提示弹出正常")

        after_clear = store.get_reassignment_draft(order.order_id, dispatcher)
        assert after_clear is None, "点击清除草稿后底层数据未删除"
        print_ok("清除草稿: 底层数据存储中草稿已删除")

        order2 = store.create_order("GUI冲突草稿测试", "", "GUIC栋", "电路维修", "高", dispatcher)
        store.dispatch_order(order2.order_id, tech1, dispatcher)
        order2_obj = store.get_order(order2.order_id)
        store.save_reassignment_draft(order2.order_id, dispatcher, tech2, "冲突场景草稿")

        inspector = store.get_user("u004")
        store.accept_order(order2.order_id, tech1)
        store.complete_order(order2.order_id, tech1)
        store.approve_order(order2.order_id, inspector)
        order2_final = store.get_order(order2.order_id)
        assert order2_final.status == Status.COMPLETED
        print_ok("工单状态流转到已完成，制造冲突场景")

        draft_still_there = store.get_reassignment_draft(order2.order_id, dispatcher)
        assert draft_still_there is not None, "冲突前草稿已丢失"

        captured["showerror"].clear()
        dlg2 = ReassignDialog(root, store, dispatcher, store.get_order(order2.order_id))
        dlg2.tree.selection_set(tech2.user_id)
        dlg2.reason_text.insert("1.0", "尝试提交冲突改派")
        dlg2._on_confirm()
        root.update()
        assert len(captured["showerror"]) >= 1, "状态变更冲突未弹错误提示"
        last_err_title, last_err_msg = captured["showerror"][-1]
        assert "草稿" in last_err_msg and "保留" in last_err_msg, \
            f"错误提示未说明草稿保留, 消息='{last_err_msg}'"
        print_ok(f"状态变更冲突: 错误提示包含草稿保留说明 - '{last_err_title}'")

        draft_after_failed_confirm = store.get_reassignment_draft(order2.order_id, dispatcher)
        assert draft_after_failed_confirm is not None, "冲突提交失败后草稿被误删"
        print_ok("冲突提交失败后草稿保留")

        order3 = store.create_order("GUI版本冲突草稿测试", "", "GUIVC栋", "空调维修", "高", dispatcher)
        store.dispatch_order(order3.order_id, tech1, dispatcher)
        order3_v1 = store.get_order(order3.order_id).version
        store.save_reassignment_draft(order3.order_id, dispatcher, tech2, "GUI版本冲突草稿v1")
        assert store.get_reassignment_draft(order3.order_id, dispatcher).order_version == order3_v1
        print_ok(f"GUI版本冲突测试: 保存草稿时工单版本 v{order3_v1}")

        drafts_path_gui = os.path.join(gui_data_dir, "reassignment_drafts.json")
        with open(drafts_path_gui, "r", encoding="utf-8") as f:
            raw_gui_before = json.load(f)
        stale_gui_draft = None
        for d in raw_gui_before:
            if d["order_id"] == order3.order_id:
                stale_gui_draft = d.copy()
                stale_gui_draft["order_version"] = order3_v1
                stale_gui_draft["target_technician_id"] = tech1.user_id
                stale_gui_draft["reason"] = "手动回写GUI测试旧版本草稿v1"
        assert stale_gui_draft is not None
        print_ok("提前备份 GUI 草稿原始内容")

        order3_obj = store.get_order(order3.order_id)
        store.reassign_order(order3.order_id, tech2, dispatcher, "他人抢先改派",
                              expected_version=order3_obj.version)
        order3_v2 = store.get_order(order3.order_id).version
        assert order3_v2 > order3_v1
        print_ok(f"他人抢先改派: 工单版本 v{order3_v1} → v{order3_v2}")

        other_gui_drafts = [d for d in raw_gui_before if d["order_id"] != order3.order_id]
        other_gui_drafts.append(stale_gui_draft)
        with open(drafts_path_gui, "w", encoding="utf-8") as f:
            json.dump(other_gui_drafts, f, ensure_ascii=False, indent=2)
        store._load_reassignment_drafts()
        stale_draft_gui = store.get_reassignment_draft(order3.order_id, dispatcher)
        assert stale_draft_gui is not None
        assert stale_draft_gui.order_version == order3_v1
        print_ok(f"模拟跨重启恢复旧草稿: 草稿v{stale_draft_gui.order_version}, 工单v{store.get_order(order3.order_id).version}")

        captured["showerror"].clear()
        order3_fresh = store.get_order(order3.order_id)
        dlg3 = ReassignDialog(root, store, dispatcher, order3_fresh)
        root.update()

        assert dlg3._loaded_draft is not None, "GUI 载入草稿后 _loaded_draft 应为非空"
        assert dlg3._loaded_draft.order_version == order3_v1
        print_ok("GUI 已正确载入旧版本草稿（_loaded_draft 缓存正确）")

        dlg3._on_confirm()
        root.update()
        assert len(captured["showerror"]) >= 1, "旧草稿版本冲突 GUI 未弹错误提示"
        last_vc_title, last_vc_msg = captured["showerror"][-1]
        assert "并发冲突" in last_vc_title
        assert "草稿和现场输入已保留" in last_vc_msg
        assert "工单数据未被覆盖" in last_vc_msg
        print_ok(f"GUI层版本冲突拦截成功: 标题='{last_vc_title}'")

        logs_vc = store.get_reassignment_logs(order3.order_id)
        assert len(logs_vc) == 1, f"版本冲突时不应追加日志, 实际有 {len(logs_vc)} 条"
        print_ok(f"GUI层版本冲突: 改派日志未被写入（仍为 {len(logs_vc)} 条）")

        draft_vc = store.get_reassignment_draft(order3.order_id, dispatcher)
        assert draft_vc is not None, "GUI版本冲突时草稿应保留"
        assert draft_vc.order_version == order3_v1
        print_ok("GUI层版本冲突: 草稿保留未清理")

        order_vc = store.get_order(order3.order_id)
        assert order_vc.assignee_id == tech2.user_id, "GUI版本冲突时工单数据不应被覆盖"
        assert order_vc.version == order3_v2
        print_ok(f"GUI层版本冲突: 工单数据未被覆盖（维修员={order_vc.assignee_name}, 版本=v{order_vc.version}）")

        assert dlg3.winfo_exists(), "GUI版本冲突时对话框不应关闭，保留现场输入"
        assert dlg3.result is None, "GUI版本冲突时 result 不应被设置"
        print_ok("GUI层版本冲突: 对话框保留，现场输入未清空")
        dlg3.destroy()

        order4 = store.create_order("GUI成功改派测试", "", "GUISUC栋", "空调维修", "高", dispatcher)
        store.dispatch_order(order4.order_id, tech1, dispatcher)
        store.save_reassignment_draft(order4.order_id, dispatcher, tech2, "成功改派草稿")
        logs_before_suc = len(store.get_reassignment_logs(order4.order_id))
        draft_before_suc = store.get_reassignment_draft(order4.order_id, dispatcher)
        assert draft_before_suc is not None
        print_ok(f"GUI成功改派: 草稿存在, 当前日志 {logs_before_suc} 条")

        captured["showerror"].clear()
        captured["showinfo"].clear()
        order4_fresh = store.get_order(order4.order_id)
        dlg4 = ReassignDialog(root, store, dispatcher, order4_fresh)
        root.update()
        dlg4._on_confirm()
        root.update()

        assert len(captured["showinfo"]) >= 1, "成功改派未弹成功提示"
        suc_title, suc_msg = captured["showinfo"][-1]
        assert "成功" in suc_title
        print_ok(f"GUI成功改派: 成功提示弹出='{suc_title}'")

        logs_after_suc = store.get_reassignment_logs(order4.order_id)
        assert len(logs_after_suc) == logs_before_suc + 1, "成功改派应写入一条新日志"
        last_log = logs_after_suc[-1]
        assert last_log.to_user_id == tech2.user_id
        assert last_log.from_user_id == tech1.user_id
        assert last_log.dispatcher_id == dispatcher.user_id
        assert last_log.reason == "成功改派草稿"
        print_ok(f"GUI成功改派: 改派日志已写入 - 从 {last_log.from_user_name} 到 {last_log.to_user_name}, 调度员={last_log.dispatcher_name}, 原因='{last_log.reason}'")

        draft_after_suc = store.get_reassignment_draft(order4.order_id, dispatcher)
        assert draft_after_suc is None, "成功改派后草稿应自动清理"
        print_ok("GUI成功改派: 对应草稿已自动清理")

        order4_final = store.get_order(order4.order_id)
        assert order4_final.assignee_id == tech2.user_id
        assert order4_final.assignee_name == tech2.name
        print_ok(f"GUI成功改派: 工单已更新 - 维修员={order4_final.assignee_name}")

        print_ok("GUI 改派草稿功能全部验证通过（自动载入、清除、版本冲突拦截、状态不可改派拦截、成功改派日志+草稿清理）")

    finally:
        messagebox.showerror = orig_showerror
        messagebox.showinfo = orig_showinfo
        messagebox.showwarning = orig_showwarning
        messagebox.askyesno = orig_askyesno
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        if os.path.exists(gui_data_dir):
            shutil.rmtree(gui_data_dir)
        if os.path.exists(gui_export_dir):
            shutil.rmtree(gui_export_dir)


def test_batch_reassignment_datastore(store):
    print_title("测试20: 批量改派预案 DataStore - 推荐、草稿、冲突检测、部分成功、结果导出")

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")
    inspector = store.get_user("u004")

    order1 = store.create_order("批量改派工单1", "", "BATCH1栋", "空调维修", "高", dispatcher)
    store.dispatch_order(order1.order_id, tech1, dispatcher)
    order2 = store.create_order("批量改派工单2", "", "BATCH2栋", "水管维修", "中", dispatcher)
    store.dispatch_order(order2.order_id, tech1, dispatcher)
    order3 = store.create_order("批量改派工单3", "", "BATCH3栋", "电路维修", "低", dispatcher)
    order3_v = store.get_order(order3.order_id).version

    order_ids = [order1.order_id, order2.order_id, order3.order_id]

    if "空调" not in tech2.skills:
        tech2.skills.append("空调")
        store._save_users()
    print_ok(f"测试20: 确保 tech2【{tech2.name}】包含空调技能: {tech2.skills}")

    items = store.generate_batch_recommendations(order_ids, dispatcher)
    assert len(items) == 3, f"应为3条推荐，实际{len(items)}"
    for it in items:
        assert it.order_id in order_ids
        assert it.target_technician_id
        assert it.reason
        assert it.match_score is not None
    print_ok(f"批量推荐生成成功: {len(items)} 条工单，每条含推荐维修员、原因、匹配分")

    for it in items:
        if it.order_id == order1.order_id:
            it.target_technician_id = tech2.user_id
            it.reason = "批量改派-调整目标"
            it.tech_skills_snapshot = list(tech2.skills)
            it.tech_schedule_snapshot = [ts.to_dict() for ts in tech2.time_slots]
            it.tech_max_parallel_snapshot = tech2.max_parallel_orders
    draft = store.save_batch_reassignment_draft(dispatcher, items)
    assert draft.draft_id.startswith("BRD")
    assert draft.dispatcher_id == dispatcher.user_id
    assert len(draft.items) == 3
    print_ok(f"批量草稿保存成功: draft_id={draft.draft_id}, 条目数={len(draft.items)}")

    loaded = store.get_batch_reassignment_draft(draft.draft_id, dispatcher)
    assert loaded is not None
    assert loaded.draft_id == draft.draft_id
    assert len(loaded.items) == 3
    print_ok("批量草稿读取成功")

    other_load = store.get_batch_reassignment_draft(draft.draft_id, inspector)
    assert other_load is None, "其他用户不应读取到不属于自己的草稿"
    print_ok("批量草稿按调度员隔离")

    all_drafts = store.get_batch_drafts_by_dispatcher(dispatcher)
    assert len(all_drafts) >= 1
    print_ok(f"按调度员查询草稿成功: 共 {len(all_drafts)} 个")

    data_dir = store.data_dir
    batch_path = os.path.join(data_dir, "batch_reassignment_drafts.json")
    assert os.path.exists(batch_path), "批量草稿持久化文件不存在"
    with open(batch_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    assert any(d["draft_id"] == draft.draft_id for d in raw)
    print_ok(f"批量草稿持久化文件存在且内容正确: {batch_path}")

    print_ok("重启 DataStore 模拟关闭应用...")
    del store
    import gc as _gc
    _gc.collect()
    store2 = DataStore(data_dir)
    dispatcher2 = store2.get_user("u001")

    restored = store2.get_batch_reassignment_draft(draft.draft_id, dispatcher2)
    assert restored is not None, "重启后批量草稿未恢复"
    assert restored.draft_id == draft.draft_id
    assert len(restored.items) == 3
    print_ok(f"跨重启批量草稿恢复成功: {restored.draft_id}")

    conflicts = store2.detect_batch_conflicts(restored)
    assert len(conflicts) == 0, "不应存在冲突"
    print_ok("重启后冲突检测: 无冲突（符合预期）")

    tech2_2 = store2.get_user("u003")
    order2_2 = store2.get_order(order2.order_id)
    store2.reassign_order(order2_2.order_id, tech2_2, dispatcher2, "他人抢先改派工单2",
                           expected_version=order2_2.version)
    order2_after = store2.get_order(order2.order_id)
    assert order2_after.assignee_id == tech2_2.user_id
    print_ok(f"模拟他人抢先改派工单2: 新维修员={order2_after.assignee_name}")

    conflicts_after = store2.detect_batch_conflicts(restored)
    assert order2.order_id in conflicts_after
    ct = conflicts_after[order2.order_id]
    assert any(c in ("version_mismatch", "status_changed") for c in ct)
    print_ok(f"冲突检测: 工单2 检测到冲突 {[c.value if hasattr(c, 'value') else c for c in ct]}")

    order3_obj = store2.get_order(order3.order_id)
    store2.dispatch_order(order3.order_id, tech2_2, dispatcher2)
    store2.accept_order(order3.order_id, tech2_2)
    store2.complete_order(order3.order_id, tech2_2)
    store2.approve_order(order3.order_id, inspector)
    order3_final = store2.get_order(order3.order_id)
    assert order3_final.status.value == "已完成"
    print_ok(f"工单3 流转到已完成")

    conflicts_more = store2.detect_batch_conflicts(restored)
    assert order3.order_id in conflicts_more
    print_ok(f"冲突检测: 工单3（已完成）检测到状态变更冲突")

    tech2_ensure = store2.get_user("u003")
    current_load_t2 = store2.get_technician_load("u003")
    tech2_ensure.max_parallel_orders = max(current_load_t2 + 5, 50)
    store2._save_users()
    load_t2 = store2.get_technician_load("u003")
    print_ok(f"测试20: 确保 tech2 容量充足，当前负载 {load_t2}/{tech2_ensure.max_parallel_orders}")

    result = store2.execute_batch_reassignment(restored, dispatcher2)
    assert result.dispatcher_id == dispatcher2.user_id
    assert result.success_count + result.skipped_count + result.failed_count == len(result.results)
    assert result.success_count >= 1, f"至少工单1应成功，实际成功数={result.success_count}"
    assert result.skipped_count >= 2, f"至少工单2和工单3应跳过，实际跳过={result.skipped_count}"
    print_ok(f"批量提交执行完成: 成功={result.success_count}, 跳过={result.skipped_count}, 失败={result.failed_count}")

    for r in result.results:
        if r.order_id == order1.order_id:
            assert r.success, f"工单1应成功，实际: success={r.success}, error={r.error_message}"
            assert r.target_technician_id == tech2.user_id
            assert r.reason == "批量改派-调整目标"
            print_ok(f"工单1改派成功: 新维修员={r.target_technician_name}, 原因={r.reason}")
        elif r.order_id == order2.order_id:
            assert r.skipped
            assert r.error_message is not None
            print_ok(f"工单2跳过（版本冲突）: {r.error_message}")
        elif r.order_id == order3.order_id:
            assert r.skipped
            assert r.error_message is not None
            print_ok(f"工单3跳过（已完成）: {r.error_message}")

    order1_logs = store2.get_reassignment_logs(order1.order_id)
    assert len(order1_logs) >= 1
    last_log = order1_logs[-1]
    assert last_log.to_user_id == tech2.user_id
    assert last_log.dispatcher_id == dispatcher2.user_id
    assert last_log.reason == "批量改派-调整目标"
    print_ok(f"成功项写入原有改派日志: 从 {last_log.from_user_name} 到 {last_log.to_user_name}")

    refreshed_draft = store2.get_batch_reassignment_draft(draft.draft_id, dispatcher2)
    if refreshed_draft:
        remaining_ids = {it.order_id for it in refreshed_draft.items}
        assert order1.order_id not in remaining_ids
        print_ok(f"成功项从草稿中移除，剩余 {len(refreshed_draft.items)} 条")
    else:
        print_ok("全部成功后草稿自动清理")

    export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_exports_batch")
    store2.set_export_dir(export_dir)
    csv_path = store2.export_batch_result_csv(result)
    json_path = store2.export_batch_result_json(result)
    assert os.path.exists(csv_path) and os.path.getsize(csv_path) > 0
    assert os.path.exists(json_path) and os.path.getsize(json_path) > 0
    print_ok(f"结果导出 CSV: {csv_path} ({os.path.getsize(csv_path)}字节)")
    print_ok(f"结果导出 JSON: {json_path} ({os.path.getsize(json_path)}字节)")

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        assert "提交人" in header
        assert "执行结果" in header
        assert "错误/跳过原因" in header
        rows = list(reader)
        assert len(rows) == 3
    print_ok("CSV导出字段正确（含提交人、执行结果、原因）")

    with open(json_path, "r", encoding="utf-8") as f:
        jdata = json.load(f)
    assert jdata["dispatcher_name"] == dispatcher2.name
    assert jdata["success_count"] == result.success_count
    assert len(jdata["results"]) == 3
    print_ok("JSON导出字段正确（含提交人、成功/跳过计数）")

    try:
        store2.generate_batch_recommendations(order_ids, inspector)
        print_fail("验收员居然能生成批量推荐！")
        assert False
    except PermissionError as e:
        print_ok(f"验收员无权生成批量推荐（符合预期）: {e}")

    try:
        store2.save_batch_reassignment_draft(inspector, items)
        print_fail("验收员居然能保存批量草稿！")
        assert False
    except PermissionError as e:
        print_ok(f"验收员无权保存批量草稿（符合预期）: {e}")

    print_ok("批量改派预案 DataStore 层全部验证通过")

    return store2


def test_gui_batch_reassignment():
    print_title("测试21: GUI 批量改派预案 - 草稿恢复、冲突标记、部分成功、结果导出")

    import tkinter as tk
    from tkinter import messagebox

    gui_data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_batch_test_data")
    if os.path.exists(gui_data_dir):
        shutil.rmtree(gui_data_dir)
    gui_export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_batch_test_exports")
    if os.path.exists(gui_export_dir):
        shutil.rmtree(gui_export_dir)

    captured = {"showerror": [], "showinfo": [], "showwarning": [], "askyesno": []}

    def fake_showerror(title, msg, **kw):
        captured["showerror"].append((title, msg))

    def fake_showinfo(title, msg, **kw):
        captured["showinfo"].append((title, msg))

    def fake_showwarning(title, msg, **kw):
        captured["showwarning"].append((title, msg))

    def fake_askyesno(title, msg, **kw):
        captured["askyesno"].append((title, msg))
        return True

    orig_showerror = messagebox.showerror
    orig_showinfo = messagebox.showinfo
    orig_showwarning = messagebox.showwarning
    orig_askyesno = messagebox.askyesno
    messagebox.showerror = fake_showerror
    messagebox.showinfo = fake_showinfo
    messagebox.showwarning = fake_showwarning
    messagebox.askyesno = fake_askyesno

    root = None
    try:
        store = DataStore(gui_data_dir)
        store.set_export_dir(gui_export_dir)
        dispatcher = store.get_user("u001")
        tech1 = store.get_user("u002")
        tech2 = store.get_user("u003")
        inspector = store.get_user("u004")

        order_a = store.create_order("GUI批量A", "", "GUIBTA", "空调维修", "高", dispatcher)
        store.dispatch_order(order_a.order_id, tech1, dispatcher)
        order_b = store.create_order("GUI批量B", "", "GUIBTB", "水管维修", "中", dispatcher)
        store.dispatch_order(order_b.order_id, tech1, dispatcher)
        order_c = store.create_order("GUI批量C", "", "GUIBTC", "电路维修", "低", dispatcher)

        if "空调" not in tech2.skills:
            tech2.skills.append("空调")
            store._save_users()
        print_ok(f"GUI测试21: 确保 tech2【{tech2.name}】包含空调技能: {tech2.skills}")

        items = store.generate_batch_recommendations([order_a.order_id, order_b.order_id, order_c.order_id], dispatcher)
        pre_draft = store.save_batch_reassignment_draft(dispatcher, items)
        print_ok(f"GUI测试: 预保存批量草稿 {pre_draft.draft_id}")

        root = tk.Tk()
        root.withdraw()
        root.update()

        from main import MaintenanceApp, BatchReassignDialog
        app = MaintenanceApp.__new__(MaintenanceApp)
        app.root = root
        app.store = store
        app.current_user = dispatcher
        app._configure_styles()

        captured["showinfo"].clear()
        captured["showwarning"].clear()
        dlg = BatchReassignDialog(root, store, dispatcher, pre_draft)
        root.update()

        assert hasattr(dlg, "draft_items")
        assert len(dlg.draft_items) == 3
        assert hasattr(dlg, "detail_tree")
        assert hasattr(dlg, "picker_tree")
        assert hasattr(dlg, "conflicts")
        print_ok("GUI批量改派对话框控件创建正常，草稿条目自动载入")

        detail_rows = dlg.detail_tree.get_children()
        assert len(detail_rows) == 3, f"详情树应显示3条，实际{len(detail_rows)}"
        print_ok(f"GUI载入草稿后详情列表显示 {len(detail_rows)} 条")

        assert hasattr(dlg, "draft_status_label")
        status_text = dlg.draft_status_label.cget("text")
        assert pre_draft.draft_id in status_text
        print_ok(f"GUI草稿状态条显示草稿编号: '{status_text[:60]}...'")

        dlg._detect_and_show_conflicts()
        root.update()
        assert len(dlg.conflicts) == 0
        print_ok("GUI冲突检测: 初始无冲突")

        order_b_obj = store.get_order(order_b.order_id)
        store.reassign_order(order_b.order_id, tech2, dispatcher, "他人抢先改派GUI-B",
                              expected_version=order_b_obj.version)
        store.dispatch_order(order_c.order_id, tech2, dispatcher)
        store.accept_order(order_c.order_id, tech2)
        store.complete_order(order_c.order_id, tech2)
        store.approve_order(order_c.order_id, inspector)
        print_ok("GUI测试: 外部修改工单B和工单C制造冲突")

        captured["showwarning"].clear()
        dlg._detect_and_show_conflicts()
        root.update()
        assert len(dlg.conflicts) >= 2, f"应检测到至少2条冲突，实际{len(dlg.conflicts)}"
        print_ok(f"GUI冲突检测: 检测到 {len(dlg.conflicts)} 条冲突，界面标记")

        for tag_name in ["conflict"]:
            tags = dlg.detail_tree.tag_configure(tag_name)
            assert tags is not None
        print_ok("GUI冲突行样式配置正常（conflict 标记）")

        if "空调" not in tech2.skills:
            tech2.skills.append("空调")
            store._save_users()
        print_ok(f"GUI测试21: 确保 tech2【{tech2.name}】包含空调技能: {tech2.skills}")

        for it in dlg.draft_items:
            if it.order_id == order_a.order_id:
                it.target_technician_id = tech2.user_id
                it.reason = "GUI批量改派-调整目标A"
                it.tech_skills_snapshot = list(tech2.skills)
                it.tech_schedule_snapshot = [ts.to_dict() for ts in tech2.time_slots]
                it.tech_max_parallel_snapshot = tech2.max_parallel_orders
                break
        current_load_t2_gui = store.get_technician_load("u003")
        tech2.max_parallel_orders = max(current_load_t2_gui + 5, 50)
        store._save_users()
        load_t2_gui = store.get_technician_load("u003")
        print_ok(f"GUI测试21: 确保 tech2 容量充足，当前负载 {load_t2_gui}/{tech2.max_parallel_orders}")
        dlg._refresh_detail_view()
        root.update()
        print_ok("GUI测试: 修改工单A的目标维修员为王维修（tech2）")

        captured["showinfo"].clear()
        captured["askyesno"].clear()
        dlg._on_submit_batch()
        root.update()

        assert len(captured["askyesno"]) >= 1
        print_ok("GUI提交前弹出确认框")

        assert dlg.last_result is not None
        result = dlg.last_result
        assert result.success_count >= 1
        assert result.skipped_count >= 2
        print_ok(f"GUI批量提交执行结果: 成功={result.success_count}, 跳过={result.skipped_count}")

        result_text = dlg.result_text.get("1.0", tk.END)
        assert "成功" in result_text
        assert "跳过" in result_text
        assert dispatcher.name in result_text
        print_ok("GUI执行结果文本显示正常（含成功/跳过计数、提交人）")

        logs_a = store.get_reassignment_logs(order_a.order_id)
        assert len(logs_a) >= 1
        print_ok(f"GUI批量提交: 成功工单已写入原有改派日志（{len(logs_a)}条）")

        captured["showinfo"].clear()
        dlg._export_result("csv")
        root.update()
        dlg._export_result("json")
        root.update()

        export_files = []
        if os.path.exists(gui_export_dir):
            export_files = os.listdir(gui_export_dir)
        batch_files = [f for f in export_files if f.startswith("batch_reassignment_")]
        assert len(batch_files) >= 2, f"应导出CSV和JSON两个文件，实际{len(batch_files)}"
        print_ok(f"GUI批量结果导出成功: {batch_files}")

        dlg.destroy()
        root.update()

        drafts_list = store.get_batch_drafts_by_dispatcher(dispatcher)
        if drafts_list:
            remaining = drafts_list[0]
            remaining_ids = {it.order_id for it in remaining.items}
            assert order_a.order_id not in remaining_ids
            print_ok(f"GUI部分成功后草稿中移除成功项，剩余 {len(remaining.items)} 条")

        captured["showinfo"].clear()
        captured["askyesno"].clear()
        app2 = MaintenanceApp.__new__(MaintenanceApp)
        app2.root = root
        app2.store = store
        app2.current_user = dispatcher
        app2._configure_styles()
        app2._on_restore_batch_draft()
        root.update()

        top_levels = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
        assert len(top_levels) >= 1
        restore_dlg = top_levels[-1]
        assert "恢复" in restore_dlg.title()
        print_ok("GUI恢复批量草稿对话框打开正常")

        for w in restore_dlg.winfo_children():
            w.destroy()
        restore_dlg.destroy()
        root.update()

        print_ok("GUI 批量改派预案全部验证通过")

    finally:
        messagebox.showerror = orig_showerror
        messagebox.showinfo = orig_showinfo
        messagebox.showwarning = orig_showwarning
        messagebox.askyesno = orig_askyesno
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        if os.path.exists(gui_data_dir):
            shutil.rmtree(gui_data_dir)
        if os.path.exists(gui_export_dir):
            shutil.rmtree(gui_export_dir)


def test_batch_realtime_validation_datastore(store):
    print_title("测试22: 批量改派实时校验 - 技能/容量失效时正确跳过、不误写日志、导出字段完整")

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")
    inspector = store.get_user("u004")

    rt_export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_exports_batch_rt")
    if os.path.exists(rt_export_dir):
        shutil.rmtree(rt_export_dir)
    os.makedirs(rt_export_dir, exist_ok=True)
    store.set_export_dir(rt_export_dir)

    order_s = store.create_order("RT技能场景", "", "RTS栋", "空调维修", "高", dispatcher)
    store.dispatch_order(order_s.order_id, tech2, dispatcher)
    order_l = store.create_order("RT容量场景", "", "RTL栋", "水管维修", "中", dispatcher)
    store.dispatch_order(order_l.order_id, tech1, dispatcher)
    order_o = store.create_order("RT正常场景", "", "RTO栋", "电路维修", "低", dispatcher)
    store.dispatch_order(order_o.order_id, tech2, dispatcher)

    items = store.generate_batch_recommendations(
        [order_s.order_id, order_l.order_id, order_o.order_id], dispatcher
    )

    for it in items:
        if it.order_id == order_s.order_id:
            it.target_technician_id = tech1.user_id
            it.reason = "技能将被删除测试"
            it.tech_skills_snapshot = list(tech1.skills)
            it.tech_schedule_snapshot = [ts.to_dict() for ts in tech1.time_slots]
            it.tech_max_parallel_snapshot = tech1.max_parallel_orders
        elif it.order_id == order_l.order_id:
            it.target_technician_id = tech2.user_id
            it.reason = "容量将满测试"
            it.tech_skills_snapshot = list(tech2.skills)
            it.tech_schedule_snapshot = [ts.to_dict() for ts in tech2.time_slots]
            it.tech_max_parallel_snapshot = tech2.max_parallel_orders
        elif it.order_id == order_o.order_id:
            it.target_technician_id = tech1.user_id
            it.reason = "正常改派测试"
            it.tech_skills_snapshot = list(tech1.skills)
            it.tech_schedule_snapshot = [ts.to_dict() for ts in tech1.time_slots]
            it.tech_max_parallel_snapshot = tech1.max_parallel_orders

    rt_draft = store.save_batch_reassignment_draft(dispatcher, items)
    print_ok(f"实时校验测试: 草稿保存成功，共 {len(rt_draft.items)} 条")

    load_t1_before = store.get_technician_load("u002")
    tech1.max_parallel_orders = max(load_t1_before + 10, 100)
    store._save_users()
    print_ok(f"实时校验测试: tech1【{tech1.name}】容量放大到 {tech1.max_parallel_orders}（当前负载 {load_t1_before}）")

    if "空调" in tech1.skills:
        tech1.skills.remove("空调")
    store._save_users()
    print_ok(f"实时校验测试: 移除 tech1【{tech1.name}】的空调技能")

    load_t2 = store.get_technician_load("u003")
    tech2.max_parallel_orders = max(load_t2, 1)
    store._save_users()
    filler_count = 0
    while store.get_technician_load("u003") < tech2.max_parallel_orders:
        try:
            f_order = store.create_order(f"RT填充{filler_count}", "", f"FL{filler_count}栋", "水管维修", "低", dispatcher)
            store.dispatch_order(f_order.order_id, tech2, dispatcher)
            store.accept_order(f_order.order_id, tech2)
            filler_count += 1
            if filler_count > 20:
                break
        except Exception:
            break
    load_t2_after = store.get_technician_load("u003")
    print_ok(
        f"实时校验测试: tech2【{tech2.name}】塞满负载，当前 {load_t2_after}/{tech2.max_parallel_orders}"
    )

    result = store.execute_batch_reassignment(rt_draft, dispatcher)
    assert result.success_count == 1, f"应只有1条成功，实际成功={result.success_count}"
    assert result.skipped_count == 2, f"应有2条跳过，实际跳过={result.skipped_count}"
    print_ok(f"实时校验测试: 批量执行结果 成功={result.success_count}, 跳过={result.skipped_count}")

    for r in result.results:
        if r.order_id == order_s.order_id:
            assert not r.success
            assert r.skipped
            assert r.error_message is not None and "缺少所需技能" in r.error_message
            assert any("skills_changed" in c for c in (r.conflict_types or []))
            print_ok(f"实时校验测试: 技能失效工单正确跳过 - {r.error_message}")
        elif r.order_id == order_l.order_id:
            assert not r.success
            assert r.skipped
            assert r.error_message is not None and "已达负载上限" in r.error_message
            assert any("capacity_changed" in c for c in (r.conflict_types or []))
            print_ok(f"实时校验测试: 容量已满工单正确跳过 - {r.error_message}")
        elif r.order_id == order_o.order_id:
            assert r.success
            assert r.target_technician_id == tech1.user_id
            print_ok(f"实时校验测试: 正常工单改派成功 - 目标={r.target_technician_name}")

    logs_s = store.get_reassignment_logs(order_s.order_id)
    logs_l = store.get_reassignment_logs(order_l.order_id)
    logs_o = store.get_reassignment_logs(order_o.order_id)
    assert len(logs_s) == 0, "技能失效工单不应写入改派日志"
    assert len(logs_l) == 0, "容量已满工单不应写入改派日志"
    assert len(logs_o) >= 1, "正常工单应写入改派日志"
    print_ok("实时校验测试: 仅成功工单写入改派日志，被跳过工单无误写")

    order_o_final = store.get_order(order_o.order_id)
    assert order_o_final.assignee_id == tech1.user_id
    order_s_final = store.get_order(order_s.order_id)
    assert order_s_final.assignee_id == tech2.user_id
    order_l_final = store.get_order(order_l.order_id)
    assert order_l_final.assignee_id == tech1.user_id
    print_ok("实时校验测试: 被跳过工单的维修员未被改动")

    remaining_draft = store.get_batch_reassignment_draft(rt_draft.draft_id, dispatcher)
    remaining_ids = {it.order_id for it in remaining_draft.items}
    assert order_o.order_id not in remaining_ids
    assert order_s.order_id in remaining_ids
    assert order_l.order_id in remaining_ids
    print_ok("实时校验测试: 成功项从草稿移除，跳过项保留供调度员调整")

    csv_path = store.export_batch_result_csv(result)
    json_path = store.export_batch_result_json(result)
    assert os.path.exists(csv_path) and os.path.getsize(csv_path) > 0
    assert os.path.exists(json_path) and os.path.getsize(json_path) > 0

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        assert "提交人" in header
        rows = list(reader)
        content_joined = ",".join(header) + "\n" + "\n".join([",".join(r) for r in rows])
        assert dispatcher.name in content_joined
        assert "缺少所需技能" in content_joined
        assert "已达负载上限" in content_joined
        assert "成功" in content_joined and "跳过" in content_joined
    with open(json_path, "r", encoding="utf-8") as f:
        json_content = f.read()
        jdata = json.loads(json_content)
    assert jdata["dispatcher_name"] == dispatcher.name
    assert json_content.count("缺少所需技能") >= 1
    assert json_content.count("已达负载上限") >= 1
    assert jdata["success_count"] == 1
    assert jdata["skipped_count"] == 2
    print_ok("实时校验测试: CSV/JSON 导出字段正确（含提交人、成功/跳过原因）")

    print_ok("批量改派实时校验（DataStore层）全部验证通过")
    return store


def test_gui_batch_realtime_validation():
    print_title("测试23: GUI 批量改派实时校验 - 技能/容量失效时用户可见跳过提示")

    import tkinter as tk
    from tkinter import messagebox

    gui_rt_data = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_batch_rt_data")
    gui_rt_export = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_batch_rt_export")
    if os.path.exists(gui_rt_data):
        shutil.rmtree(gui_rt_data)
    if os.path.exists(gui_rt_export):
        shutil.rmtree(gui_rt_export)

    captured = {"showerror": [], "showinfo": [], "showwarning": [], "askyesno": []}

    def fake_showerror(title, msg, **kw):
        captured["showerror"].append((title, msg))

    def fake_showinfo(title, msg, **kw):
        captured["showinfo"].append((title, msg))

    def fake_showwarning(title, msg, **kw):
        captured["showwarning"].append((title, msg))

    def fake_askyesno(title, msg, **kw):
        captured["askyesno"].append((title, msg))
        return True

    orig_showerror = messagebox.showerror
    orig_showinfo = messagebox.showinfo
    orig_showwarning = messagebox.showwarning
    orig_askyesno = messagebox.askyesno
    messagebox.showerror = fake_showerror
    messagebox.showinfo = fake_showinfo
    messagebox.showwarning = fake_showwarning
    messagebox.askyesno = fake_askyesno

    root = None
    try:
        store = DataStore(gui_rt_data)
        store.set_export_dir(gui_rt_export)
        dispatcher = store.get_user("u001")
        tech1 = store.get_user("u002")
        tech2 = store.get_user("u003")

        order_s = store.create_order("GUIRT技能", "", "GRTS", "空调维修", "高", dispatcher)
        store.dispatch_order(order_s.order_id, tech2, dispatcher)
        order_o = store.create_order("GUIRT正常", "", "GRTO", "电路维修", "低", dispatcher)
        store.dispatch_order(order_o.order_id, tech2, dispatcher)

        items = store.generate_batch_recommendations([order_s.order_id, order_o.order_id], dispatcher)
        for it in items:
            if it.order_id == order_s.order_id:
                it.target_technician_id = tech1.user_id
                it.reason = "GUI技能失效"
                it.tech_skills_snapshot = list(tech1.skills)
                it.tech_schedule_snapshot = [ts.to_dict() for ts in tech1.time_slots]
                it.tech_max_parallel_snapshot = tech1.max_parallel_orders
            elif it.order_id == order_o.order_id:
                it.target_technician_id = tech1.user_id
                it.reason = "GUI正常改派"
                it.tech_skills_snapshot = list(tech1.skills)
                it.tech_schedule_snapshot = [ts.to_dict() for ts in tech1.time_slots]
                it.tech_max_parallel_snapshot = tech1.max_parallel_orders
        gui_rt_draft = store.save_batch_reassignment_draft(dispatcher, items)

        load_t1_gui_rt = store.get_technician_load("u002")
        tech1.max_parallel_orders = max(load_t1_gui_rt + 5, 50)
        store._save_users()

        if "空调" in tech1.skills:
            tech1.skills.remove("空调")
        store._save_users()
        print_ok(f"GUI实时校验测试: 移除 tech1 空调技能，保留电路技能；容量放大到 {tech1.max_parallel_orders}")

        root = tk.Tk()
        root.withdraw()
        root.update()

        from main import MaintenanceApp, BatchReassignDialog
        app = MaintenanceApp.__new__(MaintenanceApp)
        app.root = root
        app.store = store
        app.current_user = dispatcher
        app._configure_styles()

        dlg = BatchReassignDialog(root, store, dispatcher, gui_rt_draft)
        root.update()

        captured["showinfo"].clear()
        captured["askyesno"].clear()
        dlg._on_submit_batch()
        root.update()

        assert dlg.last_result is not None
        result = dlg.last_result
        assert result.success_count == 1
        assert result.skipped_count == 1
        print_ok(f"GUI实时校验: 批量提交结果 成功={result.success_count}, 跳过={result.skipped_count}")

        result_text = dlg.result_text.get("1.0", tk.END)
        assert "缺少所需技能" in result_text or "技能" in result_text
        assert "跳过" in result_text and "成功" in result_text
        assert dispatcher.name in result_text
        print_ok("GUI实时校验: 结果文本框可见跳过原因、成功计数和提交人")

        logs_s = store.get_reassignment_logs(order_s.order_id)
        logs_o = store.get_reassignment_logs(order_o.order_id)
        assert len(logs_s) == 0, "GUI技能失效工单不应写入改派日志"
        assert len(logs_o) >= 1, "GUI正常工单应写入改派日志"
        print_ok("GUI实时校验: 仅成功工单写入改派日志，跳过项无误写")

        dlg.destroy()
        root.update()
        print_ok("GUI 批量改派实时校验全部验证通过")

    finally:
        messagebox.showerror = orig_showerror
        messagebox.showinfo = orig_showinfo
        messagebox.showwarning = orig_showwarning
        messagebox.askyesno = orig_askyesno
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        if os.path.exists(gui_rt_data):
            shutil.rmtree(gui_rt_data)
        if os.path.exists(gui_rt_export):
            shutil.rmtree(gui_rt_export)


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
        store = test_reassignment_drafts_basic(store)
        test_reassignment_drafts_conflicts(store)
        test_gui_startup_and_tabs()
        test_gui_reassign_drafts()
        store = test_batch_reassignment_datastore(store)
        test_gui_batch_reassignment()
        store = test_batch_realtime_validation_datastore(store)
        test_gui_batch_realtime_validation()

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
 16. GUI 回归：调度员启动、5个Tab切换、导出JSON/CSV、非法导入拒绝、无TclError
 17. 改派草稿：保存、读取、按调度员隔离、跨重启持久化、改派成功自动清理、手动删除
 18. 改派草稿冲突：权限拒绝、版本变更、状态流转 时草稿保留不覆盖
 19. GUI 改派草稿：弹窗自动载入草稿、一键清除、冲突时提示且保留草稿
 20. 批量改派 DataStore：多工单推荐、草稿跨重启、冲突检测(版本/状态/维修员)、部分成功部分跳过、日志写入、CSV/JSON结果导出、权限拒绝
 21. GUI 批量改派：草稿自动载入+冲突标记、部分成功结果展示、导出功能、恢复草稿对话框
 22. 批量改派实时校验：技能/容量/排班失效时正确跳过、不误写改派日志、不改动被跳过工单、成功项移除草稿保留跳过项、CSV/JSON导出含跳过原因
 23. GUI 批量改派实时校验：结果文本框可见技能失效跳过原因、成功/跳过计数、仅成功工单写入日志
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
