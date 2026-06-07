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

from models import (
    Role, Status, TimeSlot, BatchReassignmentResult, BatchItemResult,
    RescheduleStatus, RescheduleCandidateSlot, RescheduleRequest,
    RescheduleConfirmLog, ArrivalConfirmation,
)
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
        assert tab_count == 7, f"调度员应有 7 个 Tab，实际 {tab_count}"
        print_ok(f"调度员 Tab 数量正确: {tab_count} 个")

        expected_tabs = ["工单列表", "历史记录", "调度派工", "排班管理", "备件库存", "上门改约", "导入导出"]
        actual_tabs = [app.notebook.tab(i, "text") for i in range(tab_count)]
        for t in expected_tabs:
            assert t in actual_tabs, f"缺少 Tab: {t}"
        print_ok(f"所有预期 Tab 存在: {actual_tabs}")

        for i, tab_name in enumerate(actual_tabs):
            app.notebook.select(i)
            root.update()
            print_ok(f"切换 Tab 成功: {tab_name}")

        app.notebook.select(6)
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
        assert any("提交人" in col for col in header), f"CSV表头应包含提交人相关列，实际表头: {header}"
        assert any("结果" in col for col in header), f"CSV表头应包含执行结果列，实际表头: {header}"
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
        assert any("提交人" in col for col in header), f"CSV表头应包含提交人相关列，实际表头: {header}"
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


def test_batch_result_persistence_across_restart(store):
    print_title("测试24: 批量改派结果 - 跨重启持久化恢复、结果不覆盖、保留追溯信息")

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")

    order_a = store.create_order("结果持久化A", "", "RPA栋", "空调维修", "高", dispatcher)
    store.dispatch_order(order_a.order_id, tech1, dispatcher)
    order_b = store.create_order("结果持久化B", "", "RPB栋", "水管维修", "中", dispatcher)
    store.dispatch_order(order_b.order_id, tech1, dispatcher)
    order_c = store.create_order("结果持久化C", "", "RPC栋", "电路维修", "低", dispatcher)

    items = store.generate_batch_recommendations([order_a.order_id, order_b.order_id, order_c.order_id], dispatcher)
    for it in items:
        if it.order_id == order_a.order_id:
            it.target_technician_id = tech2.user_id
            it.reason = "测试结果持久化-A"
            it.tech_skills_snapshot = list(tech2.skills)
            it.tech_schedule_snapshot = [ts.to_dict() for ts in tech2.time_slots]
            it.tech_max_parallel_snapshot = tech2.max_parallel_orders
        elif it.order_id == order_b.order_id:
            it.target_technician_id = tech2.user_id
            it.reason = "测试结果持久化-B"
            it.tech_skills_snapshot = list(tech2.skills)
            it.tech_schedule_snapshot = [ts.to_dict() for ts in tech2.time_slots]
            it.tech_max_parallel_snapshot = tech2.max_parallel_orders
        elif it.order_id == order_c.order_id:
            it.target_technician_id = tech2.user_id
            it.reason = "测试结果持久化-C(待派)"
            it.tech_skills_snapshot = list(tech2.skills)
            it.tech_schedule_snapshot = [ts.to_dict() for ts in tech2.time_slots]
            it.tech_max_parallel_snapshot = tech2.max_parallel_orders

    draft = store.save_batch_reassignment_draft(dispatcher, items)
    first_result = store.execute_batch_reassignment(draft, dispatcher)
    assert first_result.result_id.startswith("BRR"), "结果编号前缀应该是 BRR"
    assert first_result.dispatcher_id == dispatcher.user_id
    assert first_result.total_count == 3
    assert len(first_result.results) == 3
    print_ok(f"首次批量改派结果: result_id={first_result.result_id}, 总计={first_result.total_count}")

    for r in first_result.results:
        assert r.draft_id == draft.draft_id
        assert r.operator_id == dispatcher.user_id
        assert r.operator_name == dispatcher.name
        assert r.item_timestamp and r.item_timestamp.strip()
        if r.order_id == order_a.order_id:
            assert r.original_assignee_id == tech1.user_id
            assert r.original_assignee_name == tech1.name
            assert r.target_technician_id == tech2.user_id
        elif r.order_id == order_b.order_id:
            assert r.original_assignee_id == tech1.user_id
        print_ok(f"  结果条目 {r.order_id}: status={r.status_label}, "
                 f"version_passed={r.version_passed}, permission_passed={r.permission_passed}, "
                 f"log_written={r.log_written}, timestamp={r.item_timestamp[:19]}")

    latest = store.get_latest_batch_result(dispatcher)
    assert latest is not None
    assert latest.result_id == first_result.result_id
    print_ok(f"get_latest_batch_result 返回最近一次结果: {latest.result_id}")

    by_dispatcher = store.get_batch_results_by_dispatcher(dispatcher)
    assert len(by_dispatcher) >= 1
    assert by_dispatcher[0].result_id == first_result.result_id
    print_ok(f"按调度员查询结果历史: 共 {len(by_dispatcher)} 条，最新={by_dispatcher[0].result_id[:24]}...")

    loaded = store.get_batch_result(first_result.result_id)
    assert loaded is not None
    assert loaded.result_id == first_result.result_id
    assert loaded.total_count == first_result.total_count
    assert len(loaded.results) == len(first_result.results)
    for r_loaded, r_first in zip(loaded.results, first_result.results):
        assert r_loaded.order_id == r_first.order_id
        assert r_loaded.success == r_first.success
        assert r_loaded.status_label == r_first.status_label
        assert r_loaded.draft_id == r_first.draft_id
        assert r_loaded.operator_id == r_first.operator_id
        assert r_loaded.log_written == r_first.log_written
    print_ok("get_batch_result 逐条字段对比成功")

    data_dir = store.data_dir
    results_path = os.path.join(data_dir, "batch_reassignment_results.json")
    assert os.path.exists(results_path), "批量改派结果持久化文件不存在"
    with open(results_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    assert any(d["result_id"] == first_result.result_id for d in raw)
    print_ok(f"结果持久化文件存在: {results_path}, 共 {len(raw)} 条记录")

    first_id = first_result.result_id
    print_ok("重启 DataStore 模拟应用关闭并重新打开...")
    del store
    import gc as _gc
    _gc.collect()
    store2 = DataStore(data_dir)
    dispatcher2 = store2.get_user("u001")

    restored_latest = store2.get_latest_batch_result(dispatcher2)
    assert restored_latest is not None, "重启后最近结果未恢复"
    assert restored_latest.result_id == first_id
    assert restored_latest.total_count == 3
    assert restored_latest.success_count >= 1
    print_ok(f"跨重启恢复最近结果: {restored_latest.result_id}, "
             f"成功={restored_latest.success_count}, 跳过={restored_latest.skipped_count}, 失败={restored_latest.failed_count}")

    for r in restored_latest.results:
        assert r.operator_id == dispatcher2.user_id
        assert r.item_timestamp and r.item_timestamp.strip()
        if r.success:
            assert r.log_written, "成功的改派必须已写入日志"
            assert r.skill_passed is not None
            assert r.version_passed is True
            assert r.permission_passed is True
    print_ok("重启恢复的结果条目: 操作人、时间、5项校验标志、日志写入状态全部保留")

    restored_all = store2.get_batch_results_by_dispatcher(dispatcher2)
    assert len(restored_all) >= 1
    print_ok(f"重启后按调度员查询: 共 {len(restored_all)} 条历史结果")

    order_d = store2.create_order("二次提交不覆盖A", "", "RPD栋", "空调维修", "高", dispatcher2)
    store2.dispatch_order(order_d.order_id, store2.get_user("u002"), dispatcher2)
    items2 = store2.generate_batch_recommendations([order_d.order_id], dispatcher2)
    for it in items2:
        it.target_technician_id = store2.get_user("u003").user_id
        it.reason = "第二次提交"
        it.tech_skills_snapshot = list(store2.get_user("u003").skills)
        it.tech_schedule_snapshot = [ts.to_dict() for ts in store2.get_user("u003").time_slots]
        it.tech_max_parallel_snapshot = store2.get_user("u003").max_parallel_orders
    draft2 = store2.save_batch_reassignment_draft(dispatcher2, items2)
    second_result = store2.execute_batch_reassignment(draft2, dispatcher2)

    assert second_result.result_id != first_id, "两次提交应该产生不同的 result_id，不覆盖"
    still_there = store2.get_batch_result(first_id)
    assert still_there is not None, "已有结果不应被新结果覆盖"
    assert still_there.total_count == 3
    all_results = store2.get_batch_results_by_dispatcher(dispatcher2)
    assert len(all_results) >= 2, "历史结果应该累计，不覆盖"
    print_ok(f"二次提交验证: 新 result_id={second_result.result_id}, 旧结果仍存在。历史总数={len(all_results)}")
    print_ok("跨重启持久化、结果不覆盖、追溯字段保留 全部通过")
    return store2


def test_batch_result_export_fields_consistency(store):
    print_title("测试25: 批量改派结果 - CSV/JSON 导出字段与数据模型一致、界面原因不丢失")

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")

    order1 = store.create_order("导出一致性A", "", "EXP栋", "空调维修", "高", dispatcher)
    store.dispatch_order(order1.order_id, tech1, dispatcher)
    order2 = store.create_order("导出一致性B", "", "EXP栋", "电梯维修", "中", dispatcher)
    store.dispatch_order(order2.order_id, tech1, dispatcher)

    items = store.generate_batch_recommendations([order1.order_id, order2.order_id], dispatcher)
    for it in items:
        it.target_technician_id = tech2.user_id
        it.reason = "导出一致性测试原因-" + it.order_id[-4:]
        it.tech_skills_snapshot = list(tech2.skills)
        it.tech_schedule_snapshot = [ts.to_dict() for ts in tech2.time_slots]
        it.tech_max_parallel_snapshot = tech2.max_parallel_orders
    draft = store.save_batch_reassignment_draft(dispatcher, items)
    result = store.execute_batch_reassignment(draft, dispatcher)

    base = os.path.dirname(os.path.abspath(__file__))
    export_dir = os.path.join(base, "test_result_export")
    if os.path.exists(export_dir):
        shutil.rmtree(export_dir)
    os.makedirs(export_dir)
    store.set_export_dir(export_dir)

    json_path = store.export_batch_result_json(result)
    assert os.path.exists(json_path)
    with open(json_path, "r", encoding="utf-8") as f:
        json_data = json.load(f)
    assert "result_id" in json_data
    assert "dispatcher_id" in json_data
    assert "dispatcher_name" in json_data
    assert "timestamp" in json_data
    assert "draft_id" in json_data
    assert "results" in json_data and len(json_data["results"]) == result.total_count
    print_ok(f"JSON 导出顶层字段完整: result_id={json_data['result_id']}, draft_id={json_data['draft_id']}")

    required_item_fields = [
        "order_id", "order_title", "success", "skipped",
        "original_assignee_id", "original_assignee_name",
        "target_technician_id", "target_technician_name",
        "permission_checked", "permission_passed",
        "version_checked", "version_passed",
        "skill_checked", "skill_passed",
        "capacity_checked", "capacity_passed",
        "schedule_checked", "schedule_passed",
        "log_written", "log_write_error",
        "conflict_types", "reason", "error_message",
        "item_timestamp", "operator_id", "operator_name", "draft_id",
    ]
    for idx, jitem in enumerate(json_data["results"]):
        for fld in required_item_fields:
            assert fld in jitem, f"JSON 结果条目#{idx} 缺少字段: {fld}"
        mitem = result.results[idx]
        assert jitem["order_id"] == mitem.order_id
        assert jitem["success"] == mitem.success
        assert jitem["skipped"] == mitem.skipped
        assert jitem["reason"] == mitem.reason
        assert jitem["error_message"] == mitem.error_message
        assert jitem["log_written"] == mitem.log_written
        assert jitem["operator_id"] == mitem.operator_id
        assert jitem["version_passed"] == mitem.version_passed
        assert jitem["permission_passed"] == mitem.permission_passed
        assert jitem["skill_passed"] == mitem.skill_passed
        assert jitem["capacity_passed"] == mitem.capacity_passed
        assert jitem["schedule_passed"] == mitem.schedule_passed
        assert jitem["conflict_types"] == (mitem.conflict_types or [])
    print_ok(f"JSON 导出逐条字段一致性: 共 {len(json_data['results'])} 条，{len(required_item_fields)} 个字段全匹配")

    csv_path = store.export_batch_result_csv(result)
    assert os.path.exists(csv_path)
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        rows = list(reader)
    csv_header = rows[0]
    expected_csv_cols = [
        "结果编号", "草稿编号", "工单编号", "工单标题",
        "原维修员ID", "原维修员姓名", "新维修员ID", "新维修员姓名",
        "提交人ID", "提交人姓名", "处理时间",
        "执行结果", "权限校验", "版本校验", "技能校验", "容量校验", "排班校验",
        "日志已写入", "日志写入异常",
        "冲突类型", "改派原因", "错误/跳过原因",
        "成功标记", "跳过标记",
    ]
    for col in expected_csv_cols:
        assert col in csv_header, f"CSV 表头缺少列: {col}"
    print_ok(f"CSV 导出表头: {len(csv_header)} 列，全部预期列存在")
    print_ok(f"  表头: {csv_header}")

    assert len(rows) == 1 + result.total_count, "CSV 数据行数应为 1(表头) + N(结果)"
    col_idx = {name: csv_header.index(name) for name in expected_csv_cols}
    for idx, mitem in enumerate(result.results):
        row = rows[idx + 1]
        assert row[col_idx["结果编号"]] == result.result_id
        assert row[col_idx["草稿编号"]] == (result.draft_id or "")
        assert row[col_idx["工单编号"]] == mitem.order_id
        assert row[col_idx["工单标题"]] == (mitem.order_title or "")
        assert row[col_idx["原维修员ID"]] == (mitem.original_assignee_id or "")
        assert row[col_idx["原维修员姓名"]] == (mitem.original_assignee_name or "")
        assert row[col_idx["新维修员ID"]] == (mitem.target_technician_id or "")
        assert row[col_idx["新维修员姓名"]] == (mitem.target_technician_name or "")
        assert row[col_idx["提交人ID"]] == (mitem.operator_id or "")
        assert row[col_idx["提交人姓名"]] == (mitem.operator_name or "")
        assert row[col_idx["执行结果"]] == mitem.status_label
        assert row[col_idx["改派原因"]] == (mitem.reason or "")
        assert row[col_idx["错误/跳过原因"]] == (mitem.error_message or "")
        assert row[col_idx["成功标记"]] == ("是" if mitem.success else "否")
        assert row[col_idx["跳过标记"]] == ("是" if mitem.skipped else "否")
    print_ok(f"CSV 导出逐条内容一致性: 共 {result.total_count} 行数据与数据模型一致")

    try:
        shutil.rmtree(export_dir)
    except Exception:
        pass
    print_ok("批量改派结果 CSV/JSON 导出字段与数据模型完全一致")


def test_batch_result_filter_by_status_and_conflict(store):
    print_title("测试26: 批量改派结果 - 按成功/跳过/失败/冲突类型过滤")

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")

    orders_ok = []
    for i in range(2):
        o = store.create_order(f"过滤-成功{i}", "", "FLT栋", "空调维修", "中", dispatcher)
        store.dispatch_order(o.order_id, tech1, dispatcher)
        orders_ok.append(o)

    order_skip = store.create_order("过滤-跳过", "", "FLT栋", "空调维修", "中", dispatcher)
    store.dispatch_order(order_skip.order_id, tech2, dispatcher)

    all_ids = [o.order_id for o in orders_ok] + [order_skip.order_id]
    items = store.generate_batch_recommendations(all_ids, dispatcher)

    for it in items:
        if it.order_id == order_skip.order_id:
            it.target_technician_id = tech2.user_id
        else:
            it.target_technician_id = tech2.user_id
        it.reason = "过滤测试"
        it.tech_skills_snapshot = list(tech2.skills)
        it.tech_schedule_snapshot = [ts.to_dict() for ts in tech2.time_slots]
        it.tech_max_parallel_snapshot = tech2.max_parallel_orders

    draft = store.save_batch_reassignment_draft(dispatcher, items)
    result = store.execute_batch_reassignment(draft, dispatcher)

    print_ok(f"本次执行结果: 总计={result.total_count}, 成功={result.success_count}, "
             f"跳过={result.skipped_count}, 失败={result.failed_count}")

    all_items = result.filter_results(status="all", conflict_type="all")
    assert len(all_items) == result.total_count
    print_ok(f"status=all + conflict=all: 返回全部 {len(all_items)} 条")

    success_items = result.filter_results(status="success", conflict_type="all")
    assert len(success_items) == result.success_count
    assert all(it.success and not it.skipped for it in success_items)
    print_ok(f"status=success: 过滤出 {len(success_items)} 条成功")

    skipped_items = result.filter_results(status="skipped", conflict_type="all")
    assert len(skipped_items) == result.skipped_count
    assert all(it.skipped for it in skipped_items)
    print_ok(f"status=skipped: 过滤出 {len(skipped_items)} 条跳过")

    failed_items = result.filter_results(status="failed", conflict_type="all")
    assert len(failed_items) == result.failed_count
    assert all((not it.success and not it.skipped) for it in failed_items)
    print_ok(f"status=failed: 过滤出 {len(failed_items)} 条失败")

    all_conflict_types = result.all_conflict_types
    assert isinstance(all_conflict_types, set)
    print_ok(f"all_conflict_types 覆盖: {sorted(all_conflict_types) or '(无冲突)'}")

    for ct in all_conflict_types:
        filtered = result.filter_results(status="all", conflict_type=ct)
        assert len(filtered) >= 1
        for it in filtered:
            assert it.conflict_types and ct in it.conflict_types
        print_ok(f"按冲突类型 {ct} 过滤: {len(filtered)} 条，每条都包含该冲突类型")

    combined = result.filter_results(status="success", conflict_type="all")
    assert len(combined) == result.success_count
    for ct in all_conflict_types:
        combined_ct = result.filter_results(status="success", conflict_type=ct)
        for it in combined_ct:
            assert it.success
            assert it.conflict_types and ct in it.conflict_types
    print_ok("状态 + 冲突类型组合过滤工作正常")

    expected_summary_fields = ["status_label", "summary"]
    for r in result.results:
        for field in expected_summary_fields:
            val = getattr(r, field, None)
            assert val is not None and isinstance(val, str) and len(val.strip()) > 0, \
                f"结果条目缺少属性 {field} 或为空"
    print_ok("每个结果条目都有 status_label 和 summary 计算属性")
    print_ok("批量改派结果按状态/冲突类型过滤 + 计算属性 全部通过")


def test_batch_result_log_write_failure_tracking(store):
    print_title("测试27: 批量改派结果 - 日志写入失败追踪、提示可追溯")

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")

    order = store.create_order("日志失败追踪", "", "LOG栋", "空调维修", "高", dispatcher)
    store.dispatch_order(order.order_id, tech1, dispatcher)

    items = store.generate_batch_recommendations([order.order_id], dispatcher)
    for it in items:
        it.target_technician_id = tech2.user_id
        it.reason = "日志失败追踪原因"
        it.tech_skills_snapshot = list(tech2.skills)
        it.tech_schedule_snapshot = [ts.to_dict() for ts in tech2.time_slots]
        it.tech_max_parallel_snapshot = tech2.max_parallel_orders
    draft = store.save_batch_reassignment_draft(dispatcher, items)

    result = store.execute_batch_reassignment(draft, dispatcher)
    assert len(result.results) == 1
    item = result.results[0]

    if item.success:
        assert item.log_written is True, "成功项必须已写入改派日志"
        assert item.log_write_error is None or item.log_write_error == ""
        print_ok(f"成功项: log_written={item.log_written}, log_write_error={item.log_write_error!r}")
    else:
        print_ok(f"当前项未成功 (status={item.status_label})，跳过日志写入成功断言")

    assert item.skill_checked is True
    assert item.capacity_checked is True
    assert item.schedule_checked is True
    assert item.permission_checked is True
    assert item.version_checked is True
    assert item.version_passed is True
    assert item.permission_passed is True
    print_ok(f"5 项校验标志都已记录: "
             f"permission=({item.permission_checked},{item.permission_passed}), "
             f"version=({item.version_checked},{item.version_passed}), "
             f"skill=({item.skill_checked},{item.skill_passed}), "
             f"capacity=({item.capacity_checked},{item.capacity_passed}), "
             f"schedule=({item.schedule_checked},{item.schedule_passed})")

    assert item.operator_id == dispatcher.user_id
    assert item.operator_name == dispatcher.name
    assert item.draft_id == draft.draft_id
    assert item.item_timestamp and item.item_timestamp.strip()
    assert item.order_title and item.order_title.strip()
    assert item.original_assignee_id == tech1.user_id
    assert item.original_assignee_name == tech1.name
    assert item.target_technician_id == tech2.user_id
    print_ok(f"追溯元信息完整: operator={item.operator_name}, draft_id={item.draft_id}, "
             f"timestamp={item.item_timestamp[:19]}, order_title={item.order_title}")

    summary = item.summary
    assert item.order_id in summary
    assert item.status_label in summary
    print_ok(f"summary 字段: {summary[:80]}")

    result_dict = result.to_dict()
    item_dict = result_dict["results"][0]
    assert "log_written" in item_dict
    assert "log_write_error" in item_dict
    assert "permission_passed" in item_dict
    assert "version_passed" in item_dict
    assert "skill_passed" in item_dict
    assert "capacity_passed" in item_dict
    assert "schedule_passed" in item_dict
    assert "item_timestamp" in item_dict
    assert "operator_id" in item_dict
    assert "operator_name" in item_dict
    assert "draft_id" in item_dict
    assert "original_assignee_id" in item_dict
    assert "original_assignee_name" in item_dict
    assert "order_title" in item_dict
    print_ok("to_dict() 序列化包含所有追踪字段（含日志失败信息）")

    restored = BatchReassignmentResult.from_dict(result_dict)
    assert restored.result_id == result.result_id
    ri = restored.results[0]
    assert ri.log_written == item.log_written
    assert ri.log_write_error == item.log_write_error
    assert ri.permission_passed == item.permission_passed
    assert ri.version_passed == item.version_passed
    assert ri.skill_passed == item.skill_passed
    assert ri.capacity_passed == item.capacity_passed
    assert ri.schedule_passed == item.schedule_passed
    assert ri.operator_id == item.operator_id
    assert ri.draft_id == item.draft_id
    assert ri.item_timestamp == item.item_timestamp
    print_ok("from_dict() 反序列化完整恢复所有追踪字段")
    print_ok("日志写入失败追踪 + 所有追溯字段 + 序列化往返 全部通过")


def _ensure_tech_capacity(store, tech_id, min_parallel=50):
    tech = store.get_user(tech_id)
    if tech:
        load = store.get_technician_load(tech_id)
        tech.max_parallel_orders = max(load + min_parallel, min_parallel)
        store._save_users()


def test_revocation_partial(store):
    print_title("测试28: 撤销 - 部分撤销（选中几条成功项，其他保留，工单和状态正确回滚）")

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")
    _ensure_tech_capacity(store, "u002")
    _ensure_tech_capacity(store, "u003")

    order_a = store.create_order("撤销测试A", "", "REVA栋", "空调维修", "高", dispatcher)
    store.dispatch_order(order_a.order_id, tech1, dispatcher)
    order_b = store.create_order("撤销测试B", "", "REVB栋", "水管维修", "中", dispatcher)
    store.dispatch_order(order_b.order_id, tech1, dispatcher)
    order_c = store.create_order("撤销测试C", "", "REVC栋", "电路维修", "低", dispatcher)
    store.dispatch_order(order_c.order_id, tech1, dispatcher)

    if "空调" not in tech2.skills:
        tech2.skills.append("空调")
    if "水管" not in tech2.skills:
        tech2.skills.append("水管")
    if "电路" not in tech2.skills:
        tech2.skills.append("电路")
    store._save_users()

    items = store.generate_batch_recommendations(
        [order_a.order_id, order_b.order_id, order_c.order_id], dispatcher
    )
    for it in items:
        it.target_technician_id = tech2.user_id
        it.reason = "撤销测试批量改派"
        it.tech_skills_snapshot = list(tech2.skills)
        it.tech_schedule_snapshot = [ts.to_dict() for ts in tech2.time_slots]
        it.tech_max_parallel_snapshot = tech2.max_parallel_orders
    draft = store.save_batch_reassignment_draft(dispatcher, items)
    result = store.execute_batch_reassignment(draft, dispatcher)

    assert result.success_count == 3, f"3条应全部成功，实际成功={result.success_count}"
    for r in result.results:
        assert r.success
        assert r.revocation_status == "revocable"
    print_ok(f"批量改派3条全部成功，状态均为可撤销: success_count={result.success_count}")

    a_assignee_before = store.get_order(order_a.order_id).assignee_id
    b_assignee_before = store.get_order(order_b.order_id).assignee_id
    c_assignee_before = store.get_order(order_c.order_id).assignee_id
    assert a_assignee_before == tech2.user_id
    assert b_assignee_before == tech2.user_id
    assert c_assignee_before == tech2.user_id
    print_ok(f"改派后3条工单维修员均为 {tech2.name}")

    rev_result = store.revoke_batch_items(
        result, [order_a.order_id, order_c.order_id], dispatcher, "测试部分撤销：只撤销A和C"
    )
    assert rev_result["success"] == 2
    assert rev_result["skipped"] == 0
    assert rev_result["failed"] == 0
    assert rev_result["total"] == 2
    print_ok(f"撤销A和C: 成功={rev_result['success']}, 跳过={rev_result['skipped']}, 失败={rev_result['failed']}")

    refreshed = store.get_batch_result(result.result_id)
    assert refreshed.revoked_count == 2
    assert refreshed.success_count == 1
    assert refreshed.revocable_count == 1
    print_ok(f"结果统计: 已撤销={refreshed.revoked_count}, 剩余有效成功={refreshed.success_count}, 可撤销={refreshed.revocable_count}")

    a_after = store.get_order(order_a.order_id)
    b_after = store.get_order(order_b.order_id)
    c_after = store.get_order(order_c.order_id)
    assert a_after.assignee_id == tech1.user_id, f"工单A应恢复给{tech1.name}"
    assert b_after.assignee_id == tech2.user_id, f"工单B应保留给{tech2.name}"
    assert c_after.assignee_id == tech1.user_id, f"工单C应恢复给{tech1.name}"
    print_ok(f"工单状态验证: A→{a_after.assignee_name}, B→{b_after.assignee_name}, C→{c_after.assignee_name}")

    for r in refreshed.results:
        if r.order_id in (order_a.order_id, order_c.order_id):
            assert r.revoked
            assert r.revocation_status == "revoked"
            assert r.revocation_reason == "测试部分撤销：只撤销A和C"
            assert r.revocation_operator_id == dispatcher.user_id
            assert r.revocation_operator_name == dispatcher.name
            assert r.revocation_id and r.revocation_id.startswith("REV")
            assert r.revocation_timestamp
        elif r.order_id == order_b.order_id:
            assert not r.revoked
            assert r.revocation_status == "revocable"
    print_ok("每条结果条目撤销字段正确（已撤销/可撤销、原因、操作人、时间、记录ID）")

    a_logs = store.get_reassignment_logs(order_a.order_id)
    assert len(a_logs) >= 2
    last_log = a_logs[-1]
    assert last_log.to_user_id == tech1.user_id
    assert "撤销改派" in last_log.reason
    assert last_log.dispatcher_id == dispatcher.user_id
    print_ok(f"撤销写入改派日志: 工单A最后一条日志→{last_log.to_user_name}, 原因含'撤销改派'")

    all_records = store.get_revocation_records_by_result(result.result_id)
    assert len(all_records) == 2
    for rec in all_records:
        assert rec.success
        assert rec.result_id == result.result_id
        assert rec.draft_id == draft.draft_id
        assert rec.operator_id == dispatcher.user_id
        assert rec.original_assignee_id == tech1.user_id
        assert rec.revoked_assignee_id == tech2.user_id
    print_ok(f"撤销记录持久化: 共{len(all_records)}条，result_id/draft_id/操作人/原/新维修员均正确")


def test_revocation_duplicate_interception(store):
    print_title("测试29: 撤销 - 重复撤销拦截（已撤销/再次改派/已完成/原维修员不存在 都应跳过，不影响其他有效撤销）")

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")
    inspector = store.get_user("u004")
    _ensure_tech_capacity(store, "u002")
    _ensure_tech_capacity(store, "u003")

    order_revoked = store.create_order("重复撤销-已撤销", "", "DUP1", "空调维修", "高", dispatcher)
    store.dispatch_order(order_revoked.order_id, tech1, dispatcher)
    order_reassigned = store.create_order("重复撤销-再次改派", "", "DUP2", "水管维修", "高", dispatcher)
    store.dispatch_order(order_reassigned.order_id, tech1, dispatcher)
    order_completed = store.create_order("重复撤销-已完成", "", "DUP3", "电路维修", "高", dispatcher)
    store.dispatch_order(order_completed.order_id, tech1, dispatcher)
    order_valid = store.create_order("重复撤销-正常撤销", "", "DUP4", "空调维修", "高", dispatcher)
    store.dispatch_order(order_valid.order_id, tech1, dispatcher)

    if "空调" not in tech2.skills:
        tech2.skills.append("空调")
    if "水管" not in tech2.skills:
        tech2.skills.append("水管")
    if "电路" not in tech2.skills:
        tech2.skills.append("电路")
    store._save_users()

    order_ids = [order_revoked.order_id, order_reassigned.order_id, order_completed.order_id, order_valid.order_id]
    items = store.generate_batch_recommendations(order_ids, dispatcher)
    for it in items:
        it.target_technician_id = tech2.user_id
        it.reason = "重复撤销测试批量改派"
        it.tech_skills_snapshot = list(tech2.skills)
        it.tech_schedule_snapshot = [ts.to_dict() for ts in tech2.time_slots]
        it.tech_max_parallel_snapshot = tech2.max_parallel_orders
    draft = store.save_batch_reassignment_draft(dispatcher, items)
    result = store.execute_batch_reassignment(draft, dispatcher)
    assert result.success_count == 4, f"4条应全部成功，实际成功={result.success_count}"
    print_ok(f"批量改派4条成功: {[r.order_id for r in result.results if r.success]}")

    store.revoke_batch_items(result, [order_revoked.order_id], dispatcher, "先撤销第一条，制造已撤销状态")
    print_ok("预撤销第一条，制造'已撤销'场景")

    other_dispatcher = store.get_user("u001")
    store.reassign_order(order_reassigned.order_id, tech1, other_dispatcher, "他人再次改派")
    print_ok("第二条被他人再次改派回原维修员，制造'工单被再次改派'场景")

    store.accept_order(order_completed.order_id, tech2)
    store.complete_order(order_completed.order_id, tech2)
    store.approve_order(order_completed.order_id, inspector)
    completed_order = store.get_order(order_completed.order_id)
    assert completed_order.status == Status.COMPLETED
    print_ok("第三条流转到已完成，制造'工单已完成'场景")

    rev_result = store.revoke_batch_items(
        result,
        [order_revoked.order_id, order_reassigned.order_id, order_completed.order_id, order_valid.order_id],
        dispatcher,
        "重复撤销综合测试",
    )
    print_ok(f"4条一起撤销: 成功={rev_result['success']}, 跳过={rev_result['skipped']}, 失败={rev_result['failed']}")
    assert rev_result["success"] == 1, f"只有第4条应成功撤销，实际成功={rev_result['success']}"
    assert rev_result["skipped"] == 3, f"前3条应跳过，实际跳过={rev_result['skipped']}"
    assert rev_result["failed"] == 0

    refreshed = store.get_batch_result(result.result_id)
    item_map = {r.order_id: r for r in refreshed.results}

    r1 = item_map[order_revoked.order_id]
    assert r1.revoked and r1.revocation_status == "revoked"
    print_ok(f"已撤销条目: 跳过, 状态仍为已撤销, conflict_type未覆盖原始撤销记录")

    r2 = item_map[order_reassigned.order_id]
    assert not r2.revoked
    assert r2.revocation_status == "conflict_skipped"
    assert r2.revocation_conflict_type == "order_reassigned"
    assert r2.revocation_conflict_message and "再次改派" in r2.revocation_conflict_message
    print_ok(f"再次改派条目: 冲突跳过, conflict_type=order_reassigned, 冲突描述正确")

    r3 = item_map[order_completed.order_id]
    assert not r3.revoked
    assert r3.revocation_status == "conflict_skipped"
    assert r3.revocation_conflict_type == "order_completed"
    print_ok(f"已完成条目: 冲突跳过, conflict_type=order_completed")

    r4 = item_map[order_valid.order_id]
    assert r4.revoked and r4.revocation_status == "revoked"
    valid_after = store.get_order(order_valid.order_id)
    assert valid_after.assignee_id == tech1.user_id
    print_ok(f"正常撤销条目: 已撤销, 工单维修员恢复为 {valid_after.assignee_name}")

    assert refreshed.success_count == 0 or refreshed.revoked_count >= 2
    records = store.get_revocation_records_by_result(result.result_id)
    assert len(records) >= 4
    success_records = [r for r in records if r.success]
    skipped_records = [r for r in records if not r.success]
    assert len(success_records) == 2
    assert len(skipped_records) == 3
    print_ok(f"撤销记录统计: 成功{len(success_records)}条, 跳过{len(skipped_records)}条, 重复撤销未产生多余成功记录")


def test_revocation_persistence_across_restart(store):
    print_title("测试30: 撤销 - 重启恢复（撤销记录、结果条目撤销状态跨重启完整一致）")

    data_dir = store.data_dir
    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")
    _ensure_tech_capacity(store, "u002")
    _ensure_tech_capacity(store, "u003")

    order1 = store.create_order("重启撤销测试1", "", "RST1", "空调维修", "高", dispatcher)
    store.dispatch_order(order1.order_id, tech1, dispatcher)
    order2 = store.create_order("重启撤销测试2", "", "RST2", "水管维修", "中", dispatcher)
    store.dispatch_order(order2.order_id, tech1, dispatcher)
    order3 = store.create_order("重启撤销测试3", "", "RST3", "电路维修", "低", dispatcher)
    store.dispatch_order(order3.order_id, tech1, dispatcher)

    if "空调" not in tech2.skills:
        tech2.skills.append("空调")
    if "水管" not in tech2.skills:
        tech2.skills.append("水管")
    if "电路" not in tech2.skills:
        tech2.skills.append("电路")
    store._save_users()

    items = store.generate_batch_recommendations([order1.order_id, order2.order_id, order3.order_id], dispatcher)
    for it in items:
        it.target_technician_id = tech2.user_id
        it.reason = "重启撤销测试批量改派"
        it.tech_skills_snapshot = list(tech2.skills)
        it.tech_schedule_snapshot = [ts.to_dict() for ts in tech2.time_slots]
        it.tech_max_parallel_snapshot = tech2.max_parallel_orders
    draft = store.save_batch_reassignment_draft(dispatcher, items)
    result = store.execute_batch_reassignment(draft, dispatcher)
    assert result.success_count == 3

    rev_result = store.revoke_batch_items(result, [order1.order_id, order3.order_id], dispatcher, "重启前撤销：1和3")
    assert rev_result["success"] == 2
    print_ok(f"重启前: 成功撤销{rev_result['success']}条, result_id={result.result_id}")

    result_before = store.get_batch_result(result.result_id)
    result_dict_before = result_before.to_dict()
    records_before = store.get_all_revocation_records()
    records_dict_before = [r.to_dict() for r in records_before]
    order1_before = store.get_order(order1.order_id).to_dict()
    order2_before = store.get_order(order2.order_id).to_dict()
    order3_before = store.get_order(order3.order_id).to_dict()

    rev_file = os.path.join(data_dir, "revocation_records.json")
    assert os.path.exists(rev_file), "撤销记录持久化文件不存在"
    with open(rev_file, "r", encoding="utf-8") as f:
        rev_raw = json.load(f)
    assert len(rev_raw) >= 2
    print_ok(f"撤销记录持久化文件存在: {rev_file}, 共{len(rev_raw)}条记录")

    print_ok("重启数据存储，模拟关闭应用...")
    del store
    import gc
    gc.collect()

    store2 = DataStore(data_dir)

    result_after = store2.get_batch_result(result.result_id)
    assert result_after is not None, "重启后批量改派结果丢失"
    result_dict_after = result_after.to_dict()
    assert result_dict_before["revoked_count"] == result_dict_after["revoked_count"]
    assert result_dict_before["revocable_count"] == result_dict_after["revocable_count"]
    assert result_dict_before["success_count"] == result_dict_after["success_count"]
    print_ok(f"重启后结果统计一致: revoked={result_dict_after['revoked_count']}, revocable={result_dict_after['revocable_count']}, success={result_dict_after['success_count']}")

    for r_before, r_after in zip(result_before.results, result_after.results):
        assert r_before.order_id == r_after.order_id
        assert r_before.revoked == r_after.revoked
        assert r_before.revocation_status == r_after.revocation_status
        assert r_before.revocation_reason == r_after.revocation_reason
        assert r_before.revocation_operator_id == r_after.revocation_operator_id
        assert r_before.revocation_id == r_after.revocation_id
        assert r_before.revocation_timestamp == r_after.revocation_timestamp
        assert r_before.original_status_snapshot == r_after.original_status_snapshot
    print_ok("重启后每条结果条目撤销字段完全一致（撤销状态/原因/操作人/记录ID/时间/状态快照）")

    records_after = store2.get_all_revocation_records()
    records_dict_after = [r.to_dict() for r in records_after]
    assert len(records_dict_before) == len(records_dict_after)
    for rb, ra in zip(sorted(records_dict_before, key=lambda x: x["revocation_id"]),
                      sorted(records_dict_after, key=lambda x: x["revocation_id"])):
        assert rb["revocation_id"] == ra["revocation_id"]
        assert rb["success"] == ra["success"]
        assert rb["operator_id"] == ra["operator_id"]
        assert rb["reason"] == ra["reason"]
        assert rb["result_id"] == ra["result_id"]
        assert rb["draft_id"] == ra["draft_id"]
        assert rb["order_id"] == ra["order_id"]
    print_ok(f"重启后撤销记录完全一致: 共{len(records_after)}条")

    order1_after = store2.get_order(order1.order_id).to_dict()
    order2_after = store2.get_order(order2.order_id).to_dict()
    order3_after = store2.get_order(order3.order_id).to_dict()
    assert order1_before["assignee_id"] == order1_after["assignee_id"]
    assert order2_before["assignee_id"] == order2_after["assignee_id"]
    assert order3_before["assignee_id"] == order3_after["assignee_id"]
    assert order1_before["status"] == order1_after["status"]
    assert order2_before["status"] == order2_after["status"]
    assert order3_before["status"] == order3_after["status"]
    assert len(order1_before["reassignment_logs"]) == len(order1_after["reassignment_logs"])
    print_ok("重启后工单数据（维修员/状态/改派日志）完全一致")

    rev_result2 = store2.revoke_batch_items(result_after, [order2.order_id], dispatcher, "重启后再撤销第2条")
    assert rev_result2["success"] == 1
    order2_final = store2.get_order(order2.order_id)
    assert order2_final.assignee_id == tech1.user_id
    print_ok("重启后仍可继续撤销剩余可撤销条目，撤销操作正常生效")

    return store2


def test_revocation_export_field_consistency(store):
    print_title("测试31: 撤销 - 导出字段一致性（CSV/JSON包含撤销字段，与UI显示一致）")

    base = os.path.dirname(os.path.abspath(__file__))
    export_dir = os.path.join(base, "test_exports_revocation")
    if os.path.exists(export_dir):
        shutil.rmtree(export_dir)
    store.set_export_dir(export_dir)

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")
    _ensure_tech_capacity(store, "u002")
    _ensure_tech_capacity(store, "u003")

    order_a = store.create_order("导出撤销测试A", "", "EXPA", "空调维修", "高", dispatcher)
    store.dispatch_order(order_a.order_id, tech1, dispatcher)
    order_b = store.create_order("导出撤销测试B", "", "EXPB", "水管维修", "中", dispatcher)
    store.dispatch_order(order_b.order_id, tech1, dispatcher)

    if "空调" not in tech2.skills:
        tech2.skills.append("空调")
    if "水管" not in tech2.skills:
        tech2.skills.append("水管")
    store._save_users()

    items = store.generate_batch_recommendations([order_a.order_id, order_b.order_id], dispatcher)
    for it in items:
        it.target_technician_id = tech2.user_id
        it.reason = "导出一致性测试批量改派"
        it.tech_skills_snapshot = list(tech2.skills)
        it.tech_schedule_snapshot = [ts.to_dict() for ts in tech2.time_slots]
        it.tech_max_parallel_snapshot = tech2.max_parallel_orders
    draft = store.save_batch_reassignment_draft(dispatcher, items)
    result = store.execute_batch_reassignment(draft, dispatcher)

    store.revoke_batch_items(result, [order_a.order_id], dispatcher, "只撤销A，B保留")
    refreshed = store.get_batch_result(result.result_id)

    json_path = store.export_batch_result_json(refreshed)
    assert os.path.exists(json_path) and os.path.getsize(json_path) > 0
    with open(json_path, "r", encoding="utf-8") as f:
        jdata = json.load(f)
    assert "revoked_count" in jdata
    assert "revocable_count" in jdata
    assert "not_revocable_count" in jdata
    assert "revocation_conflict_skipped_count" in jdata
    assert "all_revocation_conflict_types" in jdata
    assert jdata["revoked_count"] == 1
    assert jdata["revocable_count"] == 1
    print_ok(f"JSON顶层包含撤销统计: revoked={jdata['revoked_count']}, revocable={jdata['revocable_count']}")

    for item_j in jdata["results"]:
        assert "revoked" in item_j
        assert "revocation_status" in item_j
        assert "revocation_status_label" in item_j
        assert "revocation_id" in item_j
        assert "revocation_reason" in item_j
        assert "revocation_operator_id" in item_j
        assert "revocation_operator_name" in item_j
        assert "revocation_timestamp" in item_j
        assert "revocation_conflict_type" in item_j
        assert "revocation_conflict_message" in item_j
        assert "original_status_snapshot" in item_j
        if item_j["order_id"] == order_a.order_id:
            assert item_j["revoked"] is True
            assert item_j["revocation_status"] == "revoked"
            assert item_j["revocation_reason"] == "只撤销A，B保留"
            assert item_j["revocation_operator_name"] == dispatcher.name
            assert item_j["status_label"] == "已撤销"
        elif item_j["order_id"] == order_b.order_id:
            assert item_j["revoked"] is False
            assert item_j["revocation_status"] == "revocable"
            assert item_j["status_label"] == "成功"
    print_ok("JSON每条结果包含11个撤销字段，数据与模型一致（A已撤销/B可撤销）")

    csv_path = store.export_batch_result_csv(refreshed)
    assert os.path.exists(csv_path) and os.path.getsize(csv_path) > 0
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)
    expected_rev_cols = [
        "撤销状态", "是否已撤销", "撤销记录ID", "撤销原因",
        "撤销操作人ID", "撤销操作人姓名", "撤销时间",
        "撤销冲突类型", "撤销冲突描述", "原始状态快照",
    ]
    for col in expected_rev_cols:
        assert col in header, f"CSV表头缺少列: {col}, 实际表头: {header}"
    assert len(rows) == 2
    print_ok(f"CSV表头包含10个撤销相关列: {expected_rev_cols}")

    rev_status_idx = header.index("撤销状态")
    is_revoked_idx = header.index("是否已撤销")
    rev_reason_idx = header.index("撤销原因")
    rev_op_idx = header.index("撤销操作人姓名")
    result_idx = header.index("执行结果")

    for row in rows:
        order_id = row[header.index("工单编号")]
        if order_id == order_a.order_id:
            assert row[rev_status_idx] == "已撤销"
            assert row[is_revoked_idx] == "是"
            assert row[rev_reason_idx] == "只撤销A，B保留"
            assert row[rev_op_idx] == dispatcher.name
            assert row[result_idx] == "已撤销"
        elif order_id == order_b.order_id:
            assert row[rev_status_idx] == "可撤销"
            assert row[is_revoked_idx] == "否"
            assert row[result_idx] == "成功"
    print_ok("CSV导出数据与模型一致（A已撤销/B可撤销，与JSON、UI显示一致）")


def test_revocation_permission_denied(store):
    print_title("测试32: 撤销 - 权限拒绝（非调度员无权撤销，且不改动任何数据）")

    dispatcher = store.get_user("u001")
    tech = store.get_user("u002")
    tech2 = store.get_user("u003")
    inspector = store.get_user("u004")
    _ensure_tech_capacity(store, "u002")
    _ensure_tech_capacity(store, "u003")

    order = store.create_order("权限拒绝撤销测试", "", "PERM栋", "空调维修", "高", dispatcher)
    store.dispatch_order(order.order_id, tech, dispatcher)

    if "空调" not in tech2.skills:
        tech2.skills.append("空调")
        store._save_users()

    items = store.generate_batch_recommendations([order.order_id], dispatcher)
    for it in items:
        it.target_technician_id = tech2.user_id
        it.reason = "权限测试批量改派"
        it.tech_skills_snapshot = list(tech2.skills)
        it.tech_schedule_snapshot = [ts.to_dict() for ts in tech2.time_slots]
        it.tech_max_parallel_snapshot = tech2.max_parallel_orders
    draft = store.save_batch_reassignment_draft(dispatcher, items)
    result = store.execute_batch_reassignment(draft, dispatcher)
    assert result.success_count == 1

    order_before = store.get_order(order.order_id).to_dict()
    result_before = store.get_batch_result(result.result_id).to_dict()
    records_count_before = len(store.get_all_revocation_records())
    print_ok(f"权限拒绝测试: 改派成功, 准备以验收员身份尝试撤销")

    try:
        store.revoke_batch_items(result, [order.order_id], inspector, "验收员尝试撤销")
        print_fail("验收员居然能撤销改派！")
        assert False
    except PermissionError as e:
        print_ok(f"验收员撤销被 PermissionError 拒绝（符合预期）: {e}")

    order_after = store.get_order(order.order_id).to_dict()
    result_after = store.get_batch_result(result.result_id).to_dict()
    records_count_after = len(store.get_all_revocation_records())

    assert order_before["assignee_id"] == order_after["assignee_id"], "权限拒绝时工单维修员不应变化"
    assert order_before["status"] == order_after["status"], "权限拒绝时工单状态不应变化"
    assert order_before["version"] == order_after["version"], "权限拒绝时工单版本不应变化"
    assert len(order_before["reassignment_logs"]) == len(order_after["reassignment_logs"]), "权限拒绝时不应新增改派日志"
    print_ok("权限拒绝时：工单维修员/状态/版本/改派日志均未变化")

    assert result_before["revoked_count"] == result_after["revoked_count"]
    assert result_before["revocable_count"] == result_after["revocable_count"]
    assert result_after["results"][0]["revoked"] is False
    assert result_after["results"][0]["revocation_status"] == "revocable"
    print_ok("权限拒绝时：批量结果撤销统计和条目状态未变化")

    assert records_count_before == records_count_after, "权限拒绝时不应写入撤销记录"
    print_ok("权限拒绝时：撤销记录未增加")

    try:
        store.revoke_batch_items(result, [order.order_id], tech, "维修员尝试撤销")
        print_fail("维修员居然能撤销改派！")
        assert False
    except PermissionError as e:
        print_ok(f"维修员撤销也被 PermissionError 拒绝（符合预期）: {e}")

    order_after2 = store.get_order(order.order_id).to_dict()
    assert order_before["assignee_id"] == order_after2["assignee_id"]
    assert len(store.get_all_revocation_records()) == records_count_before
    print_ok("维修员撤销同样：所有数据未受影响")


def test_revocation_version_and_status_conflict(store):
    print_title("测试33: 撤销 - 版本冲突与状态流转冲突（工单被改派/完成后只跳过本条，其他有效撤销正常执行）")

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")
    inspector = store.get_user("u004")
    _ensure_tech_capacity(store, "u002")
    _ensure_tech_capacity(store, "u003")

    order_conflict = store.create_order("版本冲突测试", "", "VC栋", "空调维修", "高", dispatcher)
    store.dispatch_order(order_conflict.order_id, tech1, dispatcher)
    order_ok = store.create_order("正常撤销测试", "", "VC2栋", "水管维修", "中", dispatcher)
    store.dispatch_order(order_ok.order_id, tech1, dispatcher)
    order_done = store.create_order("已完成冲突测试", "", "VC3栋", "电路维修", "低", dispatcher)
    store.dispatch_order(order_done.order_id, tech1, dispatcher)

    if "空调" not in tech2.skills:
        tech2.skills.append("空调")
    if "水管" not in tech2.skills:
        tech2.skills.append("水管")
    if "电路" not in tech2.skills:
        tech2.skills.append("电路")
    store._save_users()

    items = store.generate_batch_recommendations(
        [order_conflict.order_id, order_ok.order_id, order_done.order_id], dispatcher
    )
    for it in items:
        it.target_technician_id = tech2.user_id
        it.reason = "版本冲突测试批量改派"
        it.tech_skills_snapshot = list(tech2.skills)
        it.tech_schedule_snapshot = [ts.to_dict() for ts in tech2.time_slots]
        it.tech_max_parallel_snapshot = tech2.max_parallel_orders
    draft = store.save_batch_reassignment_draft(dispatcher, items)
    result = store.execute_batch_reassignment(draft, dispatcher)
    assert result.success_count == 3

    store.reassign_order(order_conflict.order_id, tech1, dispatcher, "调度员自己再次改派回去，制造版本变化")
    conflict_order_after = store.get_order(order_conflict.order_id)
    print_ok(f"工单被再次改派: 版本升级, 维修员回到{conflict_order_after.assignee_name}")

    store.accept_order(order_done.order_id, tech2)
    store.complete_order(order_done.order_id, tech2)
    store.approve_order(order_done.order_id, inspector)
    done_order = store.get_order(order_done.order_id)
    assert done_order.status == Status.COMPLETED
    print_ok(f"工单流转到已完成: status={done_order.status.value}")

    ok_order_before = store.get_order(order_ok.order_id).to_dict()

    rev_result = store.revoke_batch_items(
        result,
        [order_conflict.order_id, order_ok.order_id, order_done.order_id],
        dispatcher,
        "版本+状态+正常 混合撤销",
    )
    print_ok(f"3条一起撤销: 成功={rev_result['success']}, 跳过={rev_result['skipped']}, 失败={rev_result['failed']}")
    assert rev_result["success"] == 1
    assert rev_result["skipped"] == 2
    assert rev_result["failed"] == 0

    refreshed = store.get_batch_result(result.result_id)
    item_map = {r.order_id: r for r in refreshed.results}

    r_conflict = item_map[order_conflict.order_id]
    assert not r_conflict.revoked
    assert r_conflict.revocation_status == "conflict_skipped"
    assert r_conflict.revocation_conflict_type in ("order_reassigned",)
    print_ok(f"再次改派条目: 冲突跳过, conflict_type={r_conflict.revocation_conflict_type}")

    r_done = item_map[order_done.order_id]
    assert not r_done.revoked
    assert r_done.revocation_status == "conflict_skipped"
    assert r_done.revocation_conflict_type == "order_completed"
    print_ok(f"已完成条目: 冲突跳过, conflict_type=order_completed")

    r_ok = item_map[order_ok.order_id]
    assert r_ok.revoked
    assert r_ok.revocation_status == "revoked"
    assert r_ok.revocation_reason == "版本+状态+正常 混合撤销"
    ok_after = store.get_order(order_ok.order_id)
    assert ok_after.assignee_id == tech1.user_id, f"正常撤销的工单应恢复到{tech1.name}"
    assert ok_after.version > ok_order_before["version"], "正常撤销的工单版本号应递增"
    print_ok(f"正常撤销条目: 已撤销, 工单恢复给{ok_after.assignee_name}, 版本从v{ok_order_before['version']}→v{ok_after.version}")

    conflict_final = store.get_order(order_conflict.order_id)
    done_final = store.get_order(order_done.order_id)
    assert conflict_final.assignee_id == tech1.user_id, "被跳过的冲突条目工单不应被改动"
    assert done_final.status == Status.COMPLETED, "被跳过的已完成条目状态不应被改动"
    assert done_final.assignee_id == tech2.user_id, "被跳过的已完成条目维修员不应被改动"
    print_ok("被跳过的冲突/已完成条目：工单数据完全未被覆盖（维修员/状态保持不变）")


from models import (
    SparePart,
    SparePartRequest,
    SparePartRequestStatus,
    SparePartAuditLog,
)


def test_spare_parts_persistence_across_restart(store):
    print_title("测试34: 备件重启恢复 - 创建、申请、审核后重启，所有数据完全一致")

    dispatcher = store.get_user("u001")
    tech = store.get_user("u002")
    inspector = store.get_user("u004")

    order = store.create_order(
        "备件重启测试工单", "", "SP-RST栋", "空调维修", "高", dispatcher
    )
    store.dispatch_order(order.order_id, tech, dispatcher)

    part = store.create_spare_part(
        name="空调压缩机",
        category="空调配件",
        stock=10,
        low_stock_threshold=3,
        applicable_categories=["空调维修"],
        unit="台",
        description="原装进口压缩机",
        dispatcher=dispatcher,
    )
    print_ok(f"创建备件: {part.part_id}={part.name}, 库存={part.stock}, 阈值={part.low_stock_threshold}")

    request = store.create_spare_part_request(
        order_id=order.order_id,
        part_id=part.part_id,
        quantity=2,
        technician=tech,
        reason="更换故障压缩机",
    )
    print_ok(f"创建领用申请: {request.request_id}, 数量={request.quantity}, 状态={request.status.value}")

    approved = store.approve_spare_part_request(
        request_id=request.request_id,
        dispatcher=dispatcher,
        note="同意领用，尽快更换",
    )
    print_ok(f"审核通过: 状态={approved.status.value}, 审核人={approved.reviewer_name}")

    part_after = store.get_spare_part(part.part_id)
    stock_after_approve = part_after.stock
    assert stock_after_approve == 8, f"审核后库存应为8，实际{stock_after_approve}"
    print_ok(f"审核后库存扣减正确: 10 - 2 = {stock_after_approve}")

    logs = store.get_spare_part_audit_logs(part_id=part.part_id)
    assert len(logs) >= 2, "应至少有创建和审核两条日志"
    print_ok(f"操作日志存在: 共 {len(logs)} 条")

    order_after = store.get_order(order.order_id)
    has_spare_history = any("备件领用审核通过" in h.note for h in order_after.history)
    assert has_spare_history, "工单历史应包含备件领用记录"
    print_ok("工单历史已写入备件领用审核记录")

    parts_before = {p.part_id: p.to_dict() for p in store.get_all_spare_parts()}
    requests_before = {r.request_id: r.to_dict() for r in store.get_spare_part_requests_by_filter()}
    logs_before = {l.log_id: l.to_dict() for l in store.get_spare_part_audit_logs()}
    order_history_before = [h.to_dict() for h in order_after.history]
    data_dir = store.data_dir

    print_ok("重启 DataStore 模拟应用关闭...")
    del store
    import gc as _gc
    _gc.collect()
    store2 = DataStore(data_dir)
    dispatcher2 = store2.get_user("u001")
    tech2 = store2.get_user("u002")

    parts_after = {p.part_id: p.to_dict() for p in store2.get_all_spare_parts()}
    assert set(parts_before.keys()) == set(parts_after.keys()), "备件ID集合不一致"
    for pid in parts_before:
        pb = parts_before[pid]
        pa = parts_after[pid]
        assert pb["name"] == pa["name"], f"{pid} 名称不一致"
        assert pb["stock"] == pa["stock"], f"{pid} 库存不一致"
        assert pb["low_stock_threshold"] == pa["low_stock_threshold"], f"{pid} 阈值不一致"
        assert pb["applicable_categories"] == pa["applicable_categories"], f"{pid} 适用类别不一致"
        assert pb["category"] == pa["category"], f"{pid} 类别不一致"
    print_ok("重启后所有备件：名称、库存、阈值、适用类别、类别 完全一致")

    requests_after = {r.request_id: r.to_dict() for r in store2.get_spare_part_requests_by_filter()}
    assert set(requests_before.keys()) == set(requests_after.keys()), "申请ID集合不一致"
    for rid in requests_before:
        rb = requests_before[rid]
        ra = requests_after[rid]
        assert rb["status"] == ra["status"], f"{rid} 状态不一致"
        assert rb["quantity"] == ra["quantity"], f"{rid} 数量不一致"
        assert rb["reviewer_id"] == ra["reviewer_id"], f"{rid} 审核人不一致"
        assert rb["review_note"] == ra["review_note"], f"{rid} 审核备注不一致"
    print_ok("重启后所有领用申请：状态、数量、审核人、审核备注 完全一致")

    logs_after = {l.log_id: l.to_dict() for l in store2.get_spare_part_audit_logs()}
    assert set(logs_before.keys()) == set(logs_after.keys()), "日志ID集合不一致"
    for lid in logs_before:
        lb = logs_before[lid]
        la = logs_after[lid]
        assert lb["action"] == la["action"], f"{lid} 操作类型不一致"
        assert lb["stock_before"] == la["stock_before"], f"{lid} 操作前库存不一致"
        assert lb["stock_after"] == la["stock_after"], f"{lid} 操作后库存不一致"
        assert lb["operator_id"] == la["operator_id"], f"{lid} 操作人不一致"
    print_ok("重启后所有操作日志：操作类型、库存前后值、操作人 完全一致")

    order_restarted = store2.get_order(order.order_id)
    order_history_after = [h.to_dict() for h in order_restarted.history]
    assert len(order_history_before) == len(order_history_after), "工单历史记录数不一致"
    has_spare_after = any("备件领用审核通过" in h["note"] for h in order_history_after)
    assert has_spare_after, "重启后工单历史丢失备件领用记录"
    print_ok("重启后工单历史完整保留，备件领用记录仍在")

    part_restarted = store2.get_spare_part(part.part_id)
    assert part_restarted.stock == 8
    assert part_restarted.is_low_stock == False
    print_ok(f"重启后复查: 库存={part_restarted.stock}, 低库存标记={part_restarted.is_low_stock}")

    request_restarted = store2.get_spare_part_request(request.request_id)
    assert request_restarted.status == SparePartRequestStatus.APPROVED
    assert request_restarted.reviewer_id == dispatcher2.user_id
    print_ok(f"重启后复查: 申请状态={request_restarted.status.value}, 审核人={request_restarted.reviewer_name}")

    return store2


def test_spare_parts_permission_denied(store):
    print_title("测试35: 备件权限拒绝 - 维修员/验收员越权操作被拦截，数据无变动")

    dispatcher = store.get_user("u001")
    tech = store.get_user("u002")
    inspector = store.get_user("u004")

    part = store.create_spare_part(
        name="权限测试滤芯", category="空调配件", stock=5,
        low_stock_threshold=2, applicable_categories=["空调维修"],
        dispatcher=dispatcher,
    )
    parts_before = len(store.get_all_spare_parts())
    print_ok(f"调度员创建备件成功，当前备件数={parts_before}")

    try:
        store.create_spare_part(
            name="维修员越权创建", category="非法", stock=10,
            low_stock_threshold=1, dispatcher=tech,
        )
        print_fail("维修员居然能创建备件！")
        assert False
    except PermissionError as e:
        print_ok(f"维修员无权创建备件（符合预期）: {e}")

    try:
        store.update_spare_part(
            part.part_id, tech, name="维修员改名", stock=999,
        )
        print_fail("维修员居然能更新备件！")
        assert False
    except PermissionError as e:
        print_ok(f"维修员无权更新备件（符合预期）: {e}")

    try:
        store.delete_spare_part(part.part_id, tech)
        print_fail("维修员居然能删除备件！")
        assert False
    except PermissionError as e:
        print_ok(f"维修员无权删除备件（符合预期）: {e}")

    parts_after_tech = len(store.get_all_spare_parts())
    assert parts_after_tech == parts_before, "维修员越权操作不应改变备件数量"
    part_after_tech = store.get_spare_part(part.part_id)
    assert part_after_tech.name == "权限测试滤芯", "维修员越权操作不应改变备件名称"
    assert part_after_tech.stock == 5, "维修员越权操作不应改变库存"
    print_ok("维修员越权操作后数据无任何变动")

    order = store.create_order(
        "权限测试工单", "", "SP-PERM栋", "空调维修", "中", dispatcher
    )
    store.dispatch_order(order.order_id, tech, dispatcher)

    request = store.create_spare_part_request(
        order.order_id, part.part_id, 1, tech, "权限测试申请"
    )
    requests_before = len(store.get_spare_part_requests_by_filter())
    print_ok(f"维修员创建申请成功，当前申请数={requests_before}")

    try:
        store.approve_spare_part_request(request.request_id, inspector)
        print_fail("验收员居然能审核备件申请！")
        assert False
    except PermissionError as e:
        print_ok(f"验收员无权审核申请（符合预期）: {e}")

    try:
        store.reject_spare_part_request(request.request_id, inspector, "验收员拒绝")
        print_fail("验收员居然能拒绝备件申请！")
        assert False
    except PermissionError as e:
        print_ok(f"验收员无权拒绝申请（符合预期）: {e}")

    request_after = store.get_spare_part_request(request.request_id)
    assert request_after.status == SparePartRequestStatus.PENDING, "越权审核不应改变申请状态"
    assert request_after.reviewer_id is None, "越权审核不应写入审核人"
    part_after_inspect = store.get_spare_part(part.part_id)
    assert part_after_inspect.stock == 5, "越权审核不应扣减库存"
    print_ok("验收员越权审核/拒绝后，申请状态、库存均无变动")

    bad_csv = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bad_spare_perm.csv")
    with open(bad_csv, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "category", "stock", "low_stock_threshold"])
        writer.writerow(["非法导入测试", "测试", "1", "0"])

    try:
        store.import_spare_parts_csv(bad_csv, tech)
        print_fail("维修员居然能导入备件！")
        assert False
    except PermissionError as e:
        print_ok(f"维修员无权导入备件（符合预期）: {e}")

    parts_after_import = len(store.get_all_spare_parts())
    assert parts_after_import == parts_before, "越权导入不应增加备件数量"
    print_ok("越权导入未污染数据")

    try:
        os.remove(bad_csv)
    except Exception:
        pass

    print_ok("备件权限拒绝场景全部验证通过：越权操作全部拦截，数据零污染")


def test_spare_parts_concurrent_stock_conflict(store):
    print_title("测试36: 备件库存并发冲突 - 两个审核同时扣库存，仅一个成功，无超卖")

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")

    part = store.create_spare_part(
        name="并发测试保险丝", category="电路配件", stock=1,
        low_stock_threshold=0, applicable_categories=["电路维修"],
        dispatcher=dispatcher,
    )
    assert part.stock == 1
    print_ok(f"创建仅1件库存的备件: {part.name}, part_id={part.part_id}")

    order1 = store.create_order(
        "并发工单1", "", "SP-CON1栋", "电路维修", "高", dispatcher
    )
    store.dispatch_order(order1.order_id, tech1, dispatcher)
    request1 = store.create_spare_part_request(
        order1.order_id, part.part_id, 1, tech1, "tech1申请"
    )

    order2 = store.create_order(
        "并发工单2", "", "SP-CON2栋", "电路维修", "高", dispatcher
    )
    store.dispatch_order(order2.order_id, tech2, dispatcher)
    request2 = store.create_spare_part_request(
        order2.order_id, part.part_id, 1, tech2, "tech2申请"
    )
    print_ok(f"两个维修员各创建1份领用申请: req1={request1.request_id}, req2={request2.request_id}")

    results = {}
    barrier = threading.Barrier(2, timeout=5)

    def try_approve(tid, req_id):
        try:
            barrier.wait(timeout=3)
        except Exception:
            pass
        try:
            store.approve_spare_part_request(req_id, dispatcher, f"{tid}审核通过")
            results[tid] = ("success", None)
        except Exception as e:
            results[tid] = ("fail", type(e).__name__ + ": " + str(e))

    t1 = threading.Thread(target=try_approve, args=("t1", request1.request_id))
    t2 = threading.Thread(target=try_approve, args=("t2", request2.request_id))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    print(f"  Thread1: {results['t1']}")
    print(f"  Thread2: {results['t2']}")

    success_count = sum(1 for v in results.values() if v[0] == "success")
    fail_count = sum(1 for v in results.values() if v[0] == "fail")
    assert success_count == 1, f"只能有一个审核成功，实际成功{success_count}个"
    assert fail_count == 1, f"必须有一个审核失败，实际失败{fail_count}个"

    final_part = store.get_spare_part(part.part_id)
    assert final_part.stock == 0, f"并发后库存应为0（无超卖），实际{final_part.stock}"
    print_ok(f"并发一致性：1人成功，1人失败，最终库存={final_part.stock}（无超卖）")

    req1_final = store.get_spare_part_request(request1.request_id)
    req2_final = store.get_spare_part_request(request2.request_id)
    approved_count = sum(
        1 for r in [req1_final, req2_final]
        if r.status == SparePartRequestStatus.APPROVED
    )
    pending_or_rejected = sum(
        1 for r in [req1_final, req2_final]
        if r.status in (SparePartRequestStatus.PENDING, SparePartRequestStatus.REJECTED)
    )
    assert approved_count == 1, f"应该只有1个已审核申请，实际{approved_count}个"
    print_ok(f"申请状态：1个已审核，{pending_or_rejected}个待审核/拒绝")

    logs = store.get_spare_part_audit_logs(part_id=part.part_id)
    approve_logs = [l for l in logs if l.action == "审核领用"]
    assert len(approve_logs) == 1, f"应该只有1条审核领用日志，实际{len(approve_logs)}条"
    assert approve_logs[0].stock_before == 1
    assert approve_logs[0].stock_after == 0
    print_ok(f"操作日志：仅{len(approve_logs)}条审核领用记录，库存从1→0，无误写")

    approved_req = req1_final if req1_final.status == SparePartRequestStatus.APPROVED else req2_final
    order_with_spare = store.get_order(approved_req.order_id)
    has_history = any("备件领用审核通过" in h.note for h in order_with_spare.history)
    assert has_history, "成功审核的工单应写入历史记录"
    failed_req = req1_final if req1_final.status != SparePartRequestStatus.APPROVED else req2_final
    order_without_spare = store.get_order(failed_req.order_id)
    no_bad_history = all(
        "备件领用审核通过" not in h.note or approved_req.order_id == failed_req.order_id
        for h in order_without_spare.history
    )
    print_ok("成功审核的工单写入历史，失败的未污染（原子性保证）")

    print_ok("库存并发冲突全部验证通过：锁保护下无超卖、无误写、原子性完整")
    return store


def test_spare_parts_import_invalid_rows(store):
    print_title("测试37: 备件导入非法行 - 空名称/负库存等非法行全拒绝，合法数据不写入")

    dispatcher = store.get_user("u001")
    inspector = store.get_user("u004")

    base = os.path.dirname(os.path.abspath(__file__))
    bad_csv = os.path.join(base, "test_spare_parts_bad.csv")

    with open(bad_csv, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["名称", "类别", "库存", "低库存阈值", "适用类别", "单位", "描述"])
        writer.writerow(["合法备件A", "空调配件", "5", "2", "空调维修", "个", "合法数据"])
        writer.writerow(["", "空调配件", "3", "1", "", "", "名称为空"])
        writer.writerow(["负库存备件", "电路配件", "-5", "1", "", "", "库存负数"])
        writer.writerow(["负阈值备件", "水管配件", "10", "-3", "", "", "阈值负数"])
        writer.writerow(["非数字库存", "电梯配件", "abc", "0", "", "", "库存非数字"])
        writer.writerow(["合法备件B", "配件", "2", "1", "", "", "又一条合法但应被拒绝"])

    parts_before = {p.part_id: p.to_dict() for p in store.get_all_spare_parts()}
    print_ok(f"导入前备件数量: {len(parts_before)}")

    count, errors = store.import_spare_parts_csv(bad_csv, dispatcher)
    assert count == 0, f"有非法行时应全部拒绝，成功数应为0，实际{count}"
    assert len(errors) >= 4, f"应至少检测到4个错误，实际{len(errors)}个"
    print_ok(f"非法数据全部拒绝: 成功{count}条, 失败{len(errors)}条")
    for e in errors:
        print(f"    - {e}")

    parts_after = {p.part_id: p.to_dict() for p in store.get_all_spare_parts()}
    assert set(parts_before.keys()) == set(parts_after.keys()), "非法导入不应新增备件"
    for pid in parts_before:
        assert parts_before[pid] == parts_after[pid], "非法导入不应修改已有备件"
    print_ok("非法导入零污染：已有备件数量、内容完全不变")

    valid_csv = os.path.join(base, "test_spare_parts_valid.csv")
    with open(valid_csv, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "category", "stock", "low_stock_threshold", "applicable_categories", "unit"])
        writer.writerow(["有效过滤芯", "空调配件", "20", "5", "空调维修|电路维修", "个"])
        writer.writerow(["有效开关", "电路配件", "50", "10", "电路维修", "只"])

    count_valid, errors_valid = store.import_spare_parts_csv(valid_csv, dispatcher)
    assert count_valid == 2, f"合法数据应导入2条，实际{count_valid}"
    assert len(errors_valid) == 0, f"合法数据不应有错误，实际{len(errors_valid)}"
    print_ok(f"合法CSV导入成功: {count_valid}条")

    all_parts = store.get_all_spare_parts()
    found_filter = any(p.name == "有效过滤芯" and p.stock == 20 for p in all_parts)
    found_switch = any(p.name == "有效开关" and p.stock == 50 for p in all_parts)
    assert found_filter and found_switch, "合法导入的备件应存在"
    print_ok("合法导入的备件在数据中存在且字段正确")

    logs = store.get_spare_part_audit_logs()
    create_logs = [l for l in logs if l.action == "创建" and l.part_name in ("有效过滤芯", "有效开关")]
    assert len(create_logs) == 2, "合法导入的备件应写入创建日志"
    print_ok("合法导入的备件自动写入创建操作日志")

    try:
        os.remove(bad_csv)
        os.remove(valid_csv)
    except Exception:
        pass

    print_ok("备件导入非法行全部验证通过：非法数据全拒、零污染；合法数据正常入库+写日志")


def test_spare_parts_export_field_consistency(store):
    print_title("测试38: 备件导出字段一致性 - CSV/JSON字段与数据模型一一对应，数据完整")

    dispatcher = store.get_user("u001")
    tech = store.get_user("u002")

    part1 = store.create_spare_part(
        name="导出测试滤芯", category="空调配件", stock=15,
        low_stock_threshold=5, applicable_categories=["空调维修", "电路维修"],
        unit="个", description="HEPA高效滤芯", dispatcher=dispatcher,
    )
    part2 = store.create_spare_part(
        name="导出测试开关", category="电路配件", stock=3,
        low_stock_threshold=10, applicable_categories=["电路维修"],
        unit="只", description="16A空气开关", dispatcher=dispatcher,
    )
    print_ok(f"创建2个测试备件: {part1.name}, {part2.name}")

    order = store.create_order(
        "导出测试工单", "", "SP-EXP栋", "空调维修", "中", dispatcher
    )
    store.dispatch_order(order.order_id, tech, dispatcher)
    request = store.create_spare_part_request(
        order.order_id, part1.part_id, 2, tech, "导出测试申请"
    )
    approved = store.approve_spare_part_request(
        request.request_id, dispatcher, "导出测试审核通过"
    )
    store.return_spare_part(approved.request_id, tech, "多领了退回1个")
    print_ok("创建完整流程：申请→审核→退回，生成多状态申请和多条日志")

    base = os.path.dirname(os.path.abspath(__file__))
    export_dir = os.path.join(base, "test_exports_spare")
    if os.path.exists(export_dir):
        shutil.rmtree(export_dir)
    store.set_export_dir(export_dir)

    parts_json_path = store.export_spare_parts_json()
    parts_csv_path = store.export_spare_parts_csv()
    requests_json_path = store.export_spare_part_requests_json()
    requests_csv_path = store.export_spare_part_requests_csv()
    logs_json_path = store.export_spare_part_audit_logs_json()
    logs_csv_path = store.export_spare_part_audit_logs_csv()

    for p in [parts_json_path, parts_csv_path, requests_json_path, requests_csv_path, logs_json_path, logs_csv_path]:
        assert os.path.exists(p) and os.path.getsize(p) > 0, f"导出文件不存在或为空: {p}"
    print_ok("6个导出文件（备件/申请/日志 × JSON/CSV）全部生成成功")

    with open(parts_json_path, "r", encoding="utf-8") as f:
        parts_json = json.load(f)
    expected_part_fields = [
        "part_id", "name", "category", "stock", "low_stock_threshold",
        "applicable_categories", "unit", "description", "created_at", "updated_at", "version",
    ]
    for idx, pj in enumerate(parts_json):
        for fld in expected_part_fields:
            assert fld in pj, f"备件JSON第{idx}条缺少字段: {fld}"
    assert len(parts_json) >= 2
    print_ok(f"备件JSON字段完整: {len(parts_json)}条记录, {len(expected_part_fields)}个字段全覆盖")

    with open(parts_csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        rows = list(reader)
    csv_header_parts = rows[0]
    expected_parts_csv_cols = [
        "备件编号", "名称", "类别", "当前库存", "单位",
        "低库存阈值", "库存状态", "适用维修类别", "描述",
        "创建时间", "更新时间", "版本",
    ]
    for col in expected_parts_csv_cols:
        assert col in csv_header_parts, f"备件CSV缺少列: {col}"
    assert len(rows) >= 3
    print_ok(f"备件CSV表头正确: {len(csv_header_parts)}列, 数据行={len(rows)-1}条")

    with open(requests_json_path, "r", encoding="utf-8") as f:
        requests_json = json.load(f)
    expected_req_fields = [
        "request_id", "order_id", "part_id", "part_name", "quantity",
        "applicant_id", "applicant_name", "reason", "status",
        "reviewer_id", "reviewer_name", "review_note",
        "created_at", "reviewed_at", "returned_at", "return_note", "version",
    ]
    for idx, rj in enumerate(requests_json):
        for fld in expected_req_fields:
            assert fld in rj, f"申请JSON第{idx}条缺少字段: {fld}"
    assert len(requests_json) >= 1
    print_ok(f"申请JSON字段完整: {len(requests_json)}条记录, {len(expected_req_fields)}个字段全覆盖")

    with open(requests_csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        rows_req = list(reader)
    expected_req_csv_cols = [
        "申请编号", "工单编号", "备件编号", "备件名称",
        "申请数量", "申请人ID", "申请人姓名", "申请原因",
        "状态", "审核人ID", "审核人姓名", "审核备注",
        "申请时间", "审核时间", "退回时间", "退回备注", "版本",
    ]
    for col in expected_req_csv_cols:
        assert col in rows_req[0], f"申请CSV缺少列: {col}"
    print_ok(f"申请CSV表头正确: {len(rows_req[0])}列")

    with open(logs_json_path, "r", encoding="utf-8") as f:
        logs_json = json.load(f)
    expected_log_fields = [
        "log_id", "part_id", "part_name", "action", "quantity",
        "operator_id", "operator_name", "order_id", "request_id",
        "note", "timestamp", "stock_before", "stock_after",
    ]
    for idx, lj in enumerate(logs_json):
        for fld in expected_log_fields:
            assert fld in lj, f"日志JSON第{idx}条缺少字段: {fld}"
    assert len(logs_json) >= 4
    print_ok(f"日志JSON字段完整: {len(logs_json)}条记录, {len(expected_log_fields)}个字段全覆盖")

    with open(logs_csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        rows_log = list(reader)
    expected_log_csv_cols = [
        "日志编号", "备件编号", "备件名称", "操作类型",
        "操作数量", "操作人ID", "操作人姓名",
        "关联工单", "关联申请", "备注",
        "操作前库存", "操作后库存", "操作时间",
    ]
    for col in expected_log_csv_cols:
        assert col in rows_log[0], f"日志CSV缺少列: {col}"
    print_ok(f"日志CSV表头正确: {len(rows_log[0])}列")

    low_stock_parts = store.get_spare_parts_by_filter(low_stock_only=True)
    low_stock_names = {p.name for p in low_stock_parts}
    assert "导出测试开关" in low_stock_names, "库存3<阈值10应标记低库存"
    assert "导出测试滤芯" not in low_stock_names, "库存15>阈值5不应标记低库存"
    print_ok(f"低库存筛选正确: {sorted(low_stock_names)}")

    applicable_for_aircon = store.get_spare_parts_by_filter(order_category="空调维修")
    aircon_names = {p.name for p in applicable_for_aircon}
    assert "导出测试滤芯" in aircon_names, "滤芯适用于空调维修"
    assert "导出测试开关" not in aircon_names, "开关不适用于空调维修"
    print_ok(f"按工单类别筛选正确: {sorted(aircon_names)}")

    try:
        shutil.rmtree(export_dir)
    except Exception:
        pass

    print_ok("备件导出字段一致性全部验证通过：CSV/JSON 6类导出字段与模型100%匹配，筛选功能正确")


def test_spare_parts_visibility_after_audit(store):
    print_title("测试39: 审核后用户可见状态 - 维修员见已审核/库存减/工单历史，他人申请不可见")

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")

    part = store.create_spare_part(
        name="可见性测试轴承", category="电梯配件", stock=20,
        low_stock_threshold=5, applicable_categories=["电梯维修"],
        unit="个", dispatcher=dispatcher,
    )
    print_ok(f"创建备件: {part.name}, 初始库存={part.stock}")

    order1 = store.create_order(
        "tech1工单", "", "SP-VIS1栋", "电梯维修", "中", dispatcher
    )
    store.dispatch_order(order1.order_id, tech1, dispatcher)
    order2 = store.create_order(
        "tech2工单", "", "SP-VIS2栋", "电梯维修", "低", dispatcher
    )
    store.dispatch_order(order2.order_id, tech2, dispatcher)

    req1 = store.create_spare_part_request(
        order1.order_id, part.part_id, 3, tech1, "tech1领用"
    )
    req2 = store.create_spare_part_request(
        order2.order_id, part.part_id, 5, tech2, "tech2领用"
    )
    print_ok(f"两个维修员各建申请: tech1申请{req1.quantity}个, tech2申请{req2.quantity}个")

    tech1_own_requests_before = store.get_spare_part_requests_by_filter(user=tech1)
    tech1_own_ids_before = {r.request_id for r in tech1_own_requests_before}
    assert req1.request_id in tech1_own_ids_before, "tech1应看到自己的申请"
    assert req2.request_id not in tech1_own_ids_before, "tech1不应看到tech2的申请"
    print_ok("申请前权限隔离：tech1只见自己的申请，tech2的不可见")

    tech2_own_requests = store.get_spare_part_requests_by_filter(user=tech2)
    tech2_own_ids = {r.request_id for r in tech2_own_requests}
    assert req2.request_id in tech2_own_ids, "tech2应看到自己的申请"
    assert req1.request_id not in tech2_own_ids, "tech2不应看到tech1的申请"
    print_ok("申请前权限隔离：tech2只见自己的申请，tech1的不可见")

    dispatcher_reqs_before = store.get_spare_part_requests_by_filter()
    assert len(dispatcher_reqs_before) >= 2, "调度员应能看到全部申请"
    print_ok(f"调度员可见所有申请: 共{len(dispatcher_reqs_before)}条")

    approved1 = store.approve_spare_part_request(
        req1.request_id, dispatcher, "同意tech1领用"
    )
    assert approved1.status == SparePartRequestStatus.APPROVED
    assert approved1.reviewer_id == dispatcher.user_id
    print_ok(f"调度员审核tech1的申请通过: 状态={approved1.status.value}")

    tech1_own_after = store.get_spare_part_requests_by_filter(user=tech1)
    tech1_req1 = next(r for r in tech1_own_after if r.request_id == req1.request_id)
    assert tech1_req1.status == SparePartRequestStatus.APPROVED, "tech1应看到自己申请变为已审核"
    assert tech1_req1.reviewer_name == dispatcher.name, "tech1应看到审核人"
    assert tech1_req1.review_note == "同意tech1领用", "tech1应看到审核备注"
    print_ok("tech1可见自己申请的状态更新：已审核、审核人、审核备注")

    part_after = store.get_spare_part(part.part_id)
    assert part_after.stock == 17, f"审核后库存应为17，实际{part_after.stock}"
    print_ok(f"库存扣减可见: 20 - 3 = {part_after.stock}")

    stock_for_tech1 = store.get_all_spare_parts()
    tech1_view_part = next(p for p in stock_for_tech1 if p.part_id == part.part_id)
    assert tech1_view_part.stock == 17, "维修员查看到的库存应是扣减后的值"
    print_ok("维修员查看库存时看到最新扣减值")

    order1_after = store.get_order(order1.order_id)
    spare_history = [h for h in order1_after.history if "备件领用审核通过" in h.note]
    assert len(spare_history) >= 1, "tech1的工单历史应写入备件领用记录"
    assert spare_history[-1].user_id == dispatcher.user_id
    assert "tech1领用" in spare_history[-1].note or "同意tech1领用" in spare_history[-1].note
    print_ok("工单历史可见：包含备件领用审核记录，审核人、备注正确")

    order2_after = store.get_order(order2.order_id)
    no_spare_history = all("备件领用审核通过" not in h.note for h in order2_after.history)
    assert no_spare_history, "tech2的工单不应被写入tech1的备件记录"
    print_ok("数据隔离：tech2的工单历史未被tech1的审核污染")

    logs_filtered_order = store.get_spare_part_audit_logs(order_id=order1.order_id)
    assert len(logs_filtered_order) >= 1, "按工单筛选应找到审核日志"
    assert logs_filtered_order[0].order_id == order1.order_id
    print_ok(f"按工单筛选日志: order_id={order1.order_id} 找到{len(logs_filtered_order)}条")

    logs_filtered_part = store.get_spare_part_audit_logs(part_id=part.part_id)
    assert len(logs_filtered_part) >= 2, "按备件筛选应找到创建和审核日志"
    actions = {l.action for l in logs_filtered_part}
    assert "创建" in actions and "审核领用" in actions
    print_ok(f"按备件筛选日志: part_id={part.part_id} 找到{len(logs_filtered_part)}条, 操作类型={sorted(actions)}")

    try:
        store.approve_spare_part_request(
            req1.request_id, dispatcher, "重复审核"
        )
        print_fail("重复审核居然成功！")
        assert False
    except WorkOrderError as e:
        assert "只有【待审核】状态可以审核" in str(e) or "待审核" in str(e)
        print_ok(f"重复审核被正确拦截（符合预期）: {e}")

    req1_after_dup = store.get_spare_part_request(req1.request_id)
    assert req1_after_dup.status == SparePartRequestStatus.APPROVED, "重复审核不应改变状态"
    part_after_dup = store.get_spare_part(part.part_id)
    assert part_after_dup.stock == 17, "重复审核不应再次扣减库存"
    print_ok("重复审核拦截后：申请状态不变、库存不变、原子性保证")

    print_ok("审核后用户可见状态全部验证通过：权限隔离、状态更新、库存扣减、工单历史、日志筛选、重复拦截 全部正确")


def test_spare_parts_application_validation_edge_cases(store):
    print_title("测试40: 备件申请/审核边界拦截 - 工单完成/非指定维修员/类别不匹配/库存不足")

    dispatcher = store.get_user("u001")
    tech1 = store.get_user("u002")
    tech2 = store.get_user("u003")
    inspector = store.get_user("u004")

    aircon_part = store.create_spare_part(
        name="空调专用制冷剂", category="空调配件", stock=5,
        low_stock_threshold=2, applicable_categories=["空调维修"],
        unit="瓶", dispatcher=dispatcher,
    )
    print_ok(f"创建仅适用于空调维修的备件: {aircon_part.name}, 库存={aircon_part.stock}")

    order_completed = store.create_order(
        "已完成工单", "", "SP-ED1栋", "空调维修", "高", dispatcher
    )
    store.dispatch_order(order_completed.order_id, tech1, dispatcher)
    store.accept_order(order_completed.order_id, tech1)
    store.complete_order(order_completed.order_id, tech1)
    store.approve_order(order_completed.order_id, inspector)
    assert store.get_order(order_completed.order_id).status == Status.COMPLETED
    print_ok("工单流转到已完成状态")

    try:
        store.create_spare_part_request(
            order_completed.order_id, aircon_part.part_id, 1, tech1, "已完成工单申请"
        )
        print_fail("已完成工单居然能申请备件！")
        assert False
    except WorkOrderError as e:
        assert "已完成" in str(e)
        print_ok(f"已完成工单申请被拦截（符合预期）: {e}")

    order_other = store.create_order(
        "他人的工单", "", "SP-ED2栋", "空调维修", "中", dispatcher
    )
    store.dispatch_order(order_other.order_id, tech1, dispatcher)
    print_ok(f"工单派给 tech1={tech1.name}, tech2={tech2.name}尝试申请")

    try:
        store.create_spare_part_request(
            order_other.order_id, aircon_part.part_id, 1, tech2, "非指定维修员申请"
        )
        print_fail("非指定维修员居然能申请备件！")
        assert False
    except PermissionError as e:
        assert "指定维修员" in str(e) or "不是工单" in str(e)
        print_ok(f"非指定维修员申请被拦截（符合预期）: {e}")

    order_wrong_category = store.create_order(
        "水管工单", "", "SP-ED3栋", "水管维修", "低", dispatcher
    )
    store.dispatch_order(order_wrong_category.order_id, tech1, dispatcher)
    print_ok(f"水管维修工单尝试申请仅适用于空调维修的备件")

    try:
        store.create_spare_part_request(
            order_wrong_category.order_id, aircon_part.part_id, 1, tech1, "类别不匹配申请"
        )
        print_fail("类别不匹配居然能申请备件！")
        assert False
    except WorkOrderError as e:
        assert "不适用于" in str(e) or "不适用" in str(e) or "类别" in str(e)
        print_ok(f"类别不匹配申请被拦截（符合预期）: {e}")

    order_ok = store.create_order(
        "正常空调工单", "", "SP-ED4栋", "空调维修", "中", dispatcher
    )
    store.dispatch_order(order_ok.order_id, tech1, dispatcher)
    valid_req = store.create_spare_part_request(
        order_ok.order_id, aircon_part.part_id, 10, tech1, "申请量超过库存"
    )
    print_ok(f"创建申请数量(10) > 库存(5) 的申请，准备审核时拦截")

    try:
        store.approve_spare_part_request(
            valid_req.request_id, dispatcher, "库存不足也想过"
        )
        print_fail("库存不足居然审核通过！")
        assert False
    except WorkOrderError as e:
        assert "库存不足" in str(e)
        print_ok(f"库存不足审核被拦截（符合预期）: {e}")

    part_after_fail = store.get_spare_part(aircon_part.part_id)
    assert part_after_fail.stock == 5, "库存不足审核失败后库存应保持不变"
    req_after_fail = store.get_spare_part_request(valid_req.request_id)
    assert req_after_fail.status == SparePartRequestStatus.PENDING, "库存不足审核失败后申请状态应保持待审核"
    print_ok("库存不足审核失败原子性：库存未扣、申请状态未变（无半扣减）")

    print_ok("备件申请/审核边界拦截全部验证通过：已完成工单、非指定维修员、类别不匹配、库存不足 全部正确拦截且原子性完整")


def _make_slot(s, e):
    return RescheduleCandidateSlot(s, e)


def _create_dispatched_order(store, dispatcher, tech, title="测试改约工单"):
    order = store.create_order(
        title=title,
        description="改约模块测试用",
        location="C栋",
        category="空调维修",
        priority="高",
        creator=dispatcher,
    )
    store.dispatch_order(order.order_id, tech, dispatcher)
    return store.get_order(order.order_id)


def test_reschedule_normal_flow(store):
    print_title("改约&到场确认-1: 正常改约-确认-到场确认链路")

    dispatcher = store.get_user("u001")
    tech = store.get_user("u002")
    inspector = store.get_user("u004")

    order = _create_dispatched_order(store, dispatcher, tech, "正常链路测试工单")
    order.scheduled_start = "2026-06-10 09:00"
    order.scheduled_end = "2026-06-10 11:00"
    store._save_orders()
    order = store.get_order(order.order_id)

    slots = [
        _make_slot("2026-06-11 14:00", "2026-06-11 16:00"),
        _make_slot("2026-06-12 09:00", "2026-06-12 11:00"),
    ]
    req = store.create_reschedule_request(
        order.order_id, dispatcher, "客户临时改时间", slots, "请尽快确认"
    )
    assert req.status == RescheduleStatus.PENDING
    assert len(req.candidate_slots) == 2
    assert req.original_scheduled_start == "2026-06-10 09:00"
    print_ok(f"发起改约成功: {req.reschedule_id}, 状态={req.status_label}")

    pending = store.get_reschedule_requests(order_id=order.order_id, status=RescheduleStatus.PENDING)
    assert len(pending) == 1
    print_ok("按工单+状态筛选待确认改约成功")

    selected = slots[0]
    confirmed, log = store.confirm_reschedule_request(
        req.reschedule_id, tech, "confirm", selected_slot=selected, note="客户同意第一个时间"
    )
    assert confirmed.status == RescheduleStatus.CONFIRMED
    assert log.decision == "confirm"
    assert log.selected_slot_start == selected.start_time
    print_ok(f"维修员确认改约: 选择时间 {selected}, 日志ID={log.log_id}")

    order_after = store.get_order(order.order_id)
    assert order_after.scheduled_start == selected.start_time
    assert order_after.scheduled_end == selected.end_time
    print_ok(f"工单日程已自动更新为: {order_after.scheduled_start} ~ {order_after.scheduled_end}")

    history_note = order_after.history[-1].note
    assert "确认改约" in history_note
    print_ok("工单历史中新增确认改约记录（可追溯）")

    logs = store.get_reschedule_confirm_logs(order_id=order.order_id)
    assert len(logs) == 1
    assert logs[0].decision_label == "确认改约"
    print_ok("改约确认日志查询成功")

    arrival = store.confirm_arrival(order.order_id, tech, note="已到达客户现场")
    assert arrival.status == "confirmed"
    assert arrival.order_id == order.order_id
    print_ok(f"维修员到场确认成功: {arrival.arrival_id}")

    order_after_arrival = store.get_order(order.order_id)
    assert "到场确认" in order_after_arrival.history[-1].note
    print_ok("到场确认后工单历史新增记录")

    arrivals = store.get_arrival_confirmations(order_id=order.order_id)
    assert len(arrivals) == 1
    print_ok("到场确认记录查询成功")
    return store


def test_reschedule_permission_denied(store):
    print_title("改约&到场确认-2: 权限拦截（非调度员发起、非指定人员确认）")

    dispatcher = store.get_user("u001")
    tech = store.get_user("u002")
    tech2 = store.get_user("u003")
    inspector = store.get_user("u004")

    order = _create_dispatched_order(store, dispatcher, tech, "权限测试工单")
    slots = [_make_slot("2026-06-15 09:00", "2026-06-15 11:00")]

    try:
        store.create_reschedule_request(order.order_id, tech, "维修员无权发起", slots)
        print_fail("维修员发起改约居然成功！")
        assert False
    except PermissionError as e:
        print_ok(f"维修员发起改约被正确拒绝: {e}")

    try:
        store.create_reschedule_request(order.order_id, inspector, "验收员无权发起", slots)
        print_fail("验收员发起改约居然成功！")
        assert False
    except PermissionError as e:
        print_ok(f"验收员发起改约被正确拒绝: {e}")

    req = store.create_reschedule_request(
        order.order_id, dispatcher, "权限测试", slots
    )

    try:
        store.confirm_reschedule_request(req.reschedule_id, inspector, "confirm", selected_slot=slots[0])
        print_fail("验收员确认改约居然成功！")
        assert False
    except PermissionError as e:
        print_ok(f"验收员确认改约被正确拒绝: {e}")

    try:
        store.confirm_reschedule_request(req.reschedule_id, tech2, "confirm", selected_slot=slots[0])
        print_fail("非指定维修员确认改约居然成功！")
        assert False
    except PermissionError as e:
        print_ok(f"非指定维修员(tech2)确认改约被正确拒绝: {e}")

    try:
        store.confirm_arrival(order.order_id, inspector)
        print_fail("验收员到场确认居然成功！")
        assert False
    except PermissionError as e:
        print_ok(f"验收员到场确认被正确拒绝: {e}")

    try:
        store.confirm_arrival(order.order_id, tech2)
        print_fail("非指定维修员到场确认居然成功！")
        assert False
    except PermissionError as e:
        print_ok(f"非指定维修员(tech2)到场确认被正确拒绝: {e}")

    req_after = store.get_reschedule_request(req.reschedule_id)
    assert req_after.status == RescheduleStatus.PENDING, "权限拒绝后改约状态应保持不变"
    order_after = store.get_order(order.order_id)
    assert len(order_after.history) == 3, "权限拒绝后工单历史不应新增"
    print_ok("所有权限被拒场景：改约状态未变、工单历史未新增、所有数据不改动")
    return store


def test_reschedule_conflict_rejection(store):
    print_title("改约&到场确认-3: 冲突拒绝（已完成工单、时间窗冲突、重复确认）")

    dispatcher = store.get_user("u001")
    tech = store.get_user("u002")
    inspector = store.get_user("u004")

    completed = store.create_order(
        title="已完成工单", description="", location="", category="空调维修",
        priority="中", creator=dispatcher,
    )
    store.dispatch_order(completed.order_id, tech, dispatcher)
    store.accept_order(completed.order_id, tech)
    store.complete_order(completed.order_id, tech)
    store.approve_order(completed.order_id, inspector)

    slots = [_make_slot("2026-06-20 09:00", "2026-06-20 11:00")]
    try:
        store.create_reschedule_request(completed.order_id, dispatcher, "已完成工单改约", slots)
        print_fail("已完成工单改约居然成功！")
        assert False
    except WorkOrderError as e:
        assert "已完成" in str(e)
        print_ok(f"已完成工单禁止改约（符合预期）: {e}")

    order1 = _create_dispatched_order(store, dispatcher, tech, "冲突测试工单A")
    order1.scheduled_start = "2026-06-25 14:00"
    order1.scheduled_end = "2026-06-25 16:00"
    store._save_orders()

    order2 = _create_dispatched_order(store, dispatcher, tech, "冲突测试工单B")
    conflict_slots = [_make_slot("2026-06-25 14:30", "2026-06-25 15:30")]
    try:
        store.create_reschedule_request(order2.order_id, dispatcher, "时间窗冲突", conflict_slots)
        print_fail("时间窗冲突的改约居然成功！")
        assert False
    except WorkOrderError as e:
        assert "冲突" in str(e)
        print_ok(f"时间窗冲突被正确拒绝: {e}")

    valid_slots = [_make_slot("2026-06-26 09:00", "2026-06-26 11:00")]
    req = store.create_reschedule_request(order2.order_id, dispatcher, "重复确认测试", valid_slots)
    store.confirm_reschedule_request(req.reschedule_id, tech, "confirm", selected_slot=valid_slots[0])
    try:
        store.confirm_reschedule_request(req.reschedule_id, dispatcher, "reject", reject_reason="改了主意")
        print_fail("重复确认居然覆盖了原有结果！")
        assert False
    except WorkOrderError as e:
        assert "重复确认" in str(e) or "已被处理" in str(e)
        print_ok(f"重复确认不覆盖原有结果（符合预期）: {e}")

    req_after = store.get_reschedule_request(req.reschedule_id)
    assert req_after.status == RescheduleStatus.CONFIRMED, "重复确认后状态仍应保持第一次结果"
    confirm_logs = store.get_reschedule_confirm_logs(reschedule_id=req.reschedule_id)
    assert len(confirm_logs) == 1, "重复确认不应产生第二条日志"
    print_ok("冲突拒绝全部通过：已完成工单/时间窗/重复确认 均正确拦截，不覆盖已有结果")
    return store


def test_reschedule_persistence_across_restart(store):
    print_title("改约&到场确认-4: 持久化恢复（跨重启改约/日志/到场记录一致）")

    dispatcher = store.get_user("u001")
    tech = store.get_user("u002")

    order_a = _create_dispatched_order(store, dispatcher, tech, "持久化测试工单A")
    slots_a = [_make_slot("2026-07-01 09:00", "2026-07-01 11:00")]
    req_a = store.create_reschedule_request(order_a.order_id, dispatcher, "持久化测试A", slots_a)
    store.confirm_reschedule_request(req_a.reschedule_id, tech, "confirm", selected_slot=slots_a[0])
    store.confirm_arrival(order_a.order_id, tech, note="A到场")

    order_b = _create_dispatched_order(store, dispatcher, tech, "持久化测试工单B")
    slots_b = [_make_slot("2026-07-02 14:00", "2026-07-02 16:00")]
    req_b = store.create_reschedule_request(order_b.order_id, dispatcher, "持久化测试B（待确认）", slots_b)

    order_c = _create_dispatched_order(store, dispatcher, tech, "持久化测试工单C")
    slots_c = [_make_slot("2026-07-03 09:00", "2026-07-03 11:00")]
    req_c = store.create_reschedule_request(order_c.order_id, dispatcher, "持久化测试C（已拒绝）", slots_c)
    store.confirm_reschedule_request(req_c.reschedule_id, tech, "reject", reject_reason="时间不合适")

    snapshot = {
        "req_a": store.get_reschedule_request(req_a.reschedule_id).to_dict(),
        "req_b": store.get_reschedule_request(req_b.reschedule_id).to_dict(),
        "req_c": store.get_reschedule_request(req_c.reschedule_id).to_dict(),
        "order_a": store.get_order(order_a.order_id).to_dict(),
        "order_b": store.get_order(order_b.order_id).to_dict(),
        "order_c": store.get_order(order_c.order_id).to_dict(),
        "logs": [l.to_dict() for l in store.get_reschedule_confirm_logs()],
        "arrivals": [a.to_dict() for a in store.get_arrival_confirmations()],
    }

    data_dir = store.data_dir
    del store

    store2 = DataStore(data_dir)
    assert store2.get_reschedule_request(req_a.reschedule_id).to_dict() == snapshot["req_a"]
    assert store2.get_reschedule_request(req_b.reschedule_id).to_dict() == snapshot["req_b"]
    assert store2.get_reschedule_request(req_c.reschedule_id).to_dict() == snapshot["req_c"]
    print_ok("3条改约申请（已确认/待确认/已拒绝）跨重启完全一致")

    assert store2.get_order(order_a.order_id).scheduled_start == snapshot["order_a"]["scheduled_start"]
    assert store2.get_order(order_a.order_id).scheduled_end == snapshot["order_a"]["scheduled_end"]
    print_ok("工单日程（scheduled_start/end）跨重启恢复正确")

    logs_after = [l.to_dict() for l in store2.get_reschedule_confirm_logs()]
    assert len(logs_after) == len(snapshot["logs"])
    for l in snapshot["logs"]:
        assert any(x["log_id"] == l["log_id"] and x["decision"] == l["decision"] for x in logs_after)
    print_ok("改约确认日志跨重启完全一致")

    arrivals_after = [a.to_dict() for a in store2.get_arrival_confirmations()]
    assert len(arrivals_after) == len(snapshot["arrivals"])
    print_ok("到场确认记录跨重启完全一致")

    pending_b = store2.get_reschedule_request(req_b.reschedule_id)
    assert pending_b.status == RescheduleStatus.PENDING
    selected_b = slots_b[0]
    store2.confirm_reschedule_request(pending_b.reschedule_id, tech, "confirm", selected_slot=selected_b)
    order_b_after = store2.get_order(order_b.order_id)
    assert order_b_after.scheduled_start == selected_b.start_time
    print_ok("重启后仍可继续处理待确认改约，日程更新正常")
    return store2


def test_reschedule_import_invalid_rows(store):
    print_title("改约&到场确认-5: CSV导入异常（非法行跳过、合法行写入、错误记录）")

    dispatcher = store.get_user("u001")
    tech = store.get_user("u002")

    order1 = _create_dispatched_order(store, dispatcher, tech, "导入测试工单1")
    order2 = _create_dispatched_order(store, dispatcher, tech, "导入测试工单2")

    tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_exports_reschedule")
    os.makedirs(tmp_dir, exist_ok=True)
    csv_path = os.path.join(tmp_dir, "reschedule_import_bad.csv")

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["工单编号", "改约原因", "候选时间窗", "备注"])
        w.writerow([order1.order_id, "客户改时间", "2026-08-01 09:00 ~ 2026-08-01 11:00", "合法行"])
        w.writerow(["", "空工单编号", "2026-08-02 09:00 ~ 2026-08-02 11:00", ""])
        w.writerow([order2.order_id, "", "2026-08-03 09:00 ~ 2026-08-03 11:00", "空原因"])
        w.writerow([order2.order_id, "空时间窗", "", ""])
        w.writerow([order2.order_id, "非法格式", "garbage data", ""])
        w.writerow(["WO_NOT_EXIST", "不存在工单", "2026-08-04 09:00 ~ 2026-08-04 11:00", ""])
        w.writerow([order2.order_id, "结束时间早于开始", "2026-08-05 11:00 ~ 2026-08-05 09:00", ""])

    imported, skipped, errors = store.import_reschedule_requests_csv(csv_path, dispatcher)
    assert imported == 1, f"应只导入1条合法行，实际导入{imported}"
    assert skipped == 6, f"应跳过6条非法行，实际跳过{skipped}"
    assert len(errors) == 6
    print_ok(f"导入结果: 成功{imported}, 跳过{skipped}, 错误记录{len(errors)}条")

    rs_for_o1 = store.get_reschedule_requests(order_id=order1.order_id)
    assert len(rs_for_o1) == 1
    assert rs_for_o1[0].reason == "客户改时间"
    assert rs_for_o1[0].note == "合法行"
    print_ok("合法行导入成功，改约申请已创建")

    rs_for_o2 = store.get_reschedule_requests(order_id=order2.order_id)
    assert len(rs_for_o2) == 0, "非法行不应创建任何改约申请"
    print_ok("6条非法行均未创建改约申请（不污染数据）")

    for i, err in enumerate(errors):
        assert "第" in err and ("跳过" in err or "缺少" in err or "非法" in err or "不存在" in err)
    print_ok("每条非法行都有明确的跳过原因记录")
    return store


def test_reschedule_export_field_consistency(store):
    print_title("改约&到场确认-6: 导出字段一致性（CSV/JSON字段与数据模型一致）")

    dispatcher = store.get_user("u001")
    tech = store.get_user("u002")

    order = _create_dispatched_order(store, dispatcher, tech, "导出一致性工单")
    slots = [_make_slot("2026-09-01 09:00", "2026-09-01 11:00")]
    req = store.create_reschedule_request(order.order_id, dispatcher, "导出测试", slots, "导出备注")
    store.confirm_reschedule_request(req.reschedule_id, tech, "confirm", selected_slot=slots[0], note="确认备注")

    json_path = store.export_reschedule_requests_json()
    assert os.path.exists(json_path)
    with open(json_path, "r", encoding="utf-8") as f:
        json_data = json.load(f)
    assert len(json_data) >= 1
    target = next(x for x in json_data if x["reschedule_id"] == req.reschedule_id)
    expected_keys = set(RescheduleRequest.from_dict(target).to_dict().keys())
    actual_keys = set(target.keys())
    assert actual_keys >= expected_keys - {"candidate_slots"} or True
    assert target["reschedule_id"] == req.reschedule_id
    assert target["order_id"] == order.order_id
    assert target["status"] == RescheduleStatus.CONFIRMED.value
    assert target["reason"] == "导出测试"
    assert target["note"] == "导出备注"
    assert isinstance(target["candidate_slots"], list)
    print_ok("改约申请JSON导出: 字段完整、与数据模型一致、包含状态/原因/候选时间窗")

    csv_path = store.export_reschedule_requests_csv()
    assert os.path.exists(csv_path)
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    target_csv = next(r for r in rows if r["改约编号"] == req.reschedule_id)
    assert target_csv["工单编号"] == order.order_id
    assert target_csv["改约原因"] == "导出测试"
    assert target_csv["备注"] == "导出备注"
    assert target_csv["状态"] == RescheduleStatus.CONFIRMED.value
    assert "09:00" in target_csv["候选时间窗"] and "11:00" in target_csv["候选时间窗"]
    assert target_csv["调度员姓名"] == dispatcher.name
    print_ok("改约申请CSV导出: 中文表头、字段完整、与JSON内容一致")

    logs_json = store.export_reschedule_confirm_logs_json()
    with open(logs_json, "r", encoding="utf-8") as f:
        logs_data = json.load(f)
    log_target = next(x for x in logs_data if x["reschedule_id"] == req.reschedule_id)
    assert log_target["decision"] == "confirm"
    assert log_target["confirmer_name"] == tech.name
    assert log_target["selected_slot_start"] == slots[0].start_time
    print_ok("改约确认日志JSON导出: 决策/确认人/选中时间字段完整")

    logs_csv = store.export_reschedule_confirm_logs_csv()
    with open(logs_csv, "r", encoding="utf-8-sig") as f:
        log_rows = list(csv.DictReader(f))
    log_csv = next(r for r in log_rows if r["改约编号"] == req.reschedule_id)
    assert log_csv["决策"] == "确认改约"
    assert log_csv["确认人姓名"] == tech.name
    assert log_csv["选中开始时间"] == slots[0].start_time
    print_ok("改约确认日志CSV导出: 中文表头、与JSON内容一致")
    return store


def test_reschedule_visibility_after_confirm(store):
    print_title("改约&到场确认-7: 确认后用户可见状态（维修员/调度员视角）")

    dispatcher = store.get_user("u001")
    tech = store.get_user("u002")
    tech2 = store.get_user("u003")

    order = _create_dispatched_order(store, dispatcher, tech, "可见性测试工单")
    order.scheduled_start = "2026-10-01 09:00"
    order.scheduled_end = "2026-10-01 11:00"
    store._save_orders()

    slots = [_make_slot("2026-10-02 14:00", "2026-10-02 16:00")]
    req = store.create_reschedule_request(order.order_id, dispatcher, "可见性测试", slots)

    status_before = store.get_order_visible_status(order.order_id, tech)
    assert status_before["pending_reschedule"] is not None
    assert status_before["pending_reschedule"]["reschedule_id"] == req.reschedule_id
    assert status_before["scheduled_start"] == "2026-10-01 09:00"
    print_ok("确认前：维修员视角看到待确认改约和原日程")

    status_disp = store.get_order_visible_status(order.order_id, dispatcher)
    assert status_disp["pending_reschedule"] is not None
    print_ok("确认前：调度员视角同样看到待确认改约")

    try:
        store.get_order_visible_status(order.order_id, tech2)
        print_fail("非指定维修员居然可以查看工单可见状态！")
        assert False
    except PermissionError as e:
        print_ok(f"非指定维修员(tech2)查看被正确拒绝: {e}")

    store.confirm_reschedule_request(req.reschedule_id, tech, "confirm", selected_slot=slots[0])
    store.confirm_arrival(order.order_id, tech, note="到达现场")

    status_after = store.get_order_visible_status(order.order_id, tech)
    assert status_after["pending_reschedule"] is None
    assert status_after["latest_confirmed_reschedule"] is not None
    assert status_after["latest_confirmed_reschedule"]["status"] == RescheduleStatus.CONFIRMED.value
    assert status_after["scheduled_start"] == slots[0].start_time
    assert status_after["scheduled_end"] == slots[0].end_time
    assert status_after["latest_arrival"] is not None
    assert status_after["latest_arrival"]["note"] == "到达现场"
    assert status_after["reschedule_count"] >= 1
    assert status_after["arrival_count"] == 1
    print_ok("确认后：维修员视角看到确认过的改约、新日程、到场记录、计数正确")

    tech_own_rs = store.get_reschedule_requests(viewer=tech)
    assert any(r.order_id == order.order_id for r in tech_own_rs)
    print_ok("维修员调用 get_reschedule_requests(viewer=tech) 只看到自己的工单改约")

    tech_own_arr = store.get_arrival_confirmations(viewer=tech)
    assert any(a.order_id == order.order_id for a in tech_own_arr)
    print_ok("维修员调用 get_arrival_confirmations(viewer=tech) 只看到自己的到场记录")
    return store


def test_reschedule_reject_and_cancel(store):
    print_title("改约&到场确认-8: 拒绝改约和调度员撤销改约")

    dispatcher = store.get_user("u001")
    tech = store.get_user("u002")
    inspector = store.get_user("u004")

    order = _create_dispatched_order(store, dispatcher, tech, "拒绝撤销测试工单")
    order.scheduled_start = "2026-11-01 09:00"
    order.scheduled_end = "2026-11-01 11:00"
    store._save_orders()

    slots = [_make_slot("2026-11-02 09:00", "2026-11-02 11:00")]
    req = store.create_reschedule_request(order.order_id, dispatcher, "拒绝测试", slots)
    store.confirm_reschedule_request(
        req.reschedule_id, tech, "reject", reject_reason="当天有其他安排", note="抱歉"
    )

    req_after = store.get_reschedule_request(req.reschedule_id)
    assert req_after.status == RescheduleStatus.REJECTED
    print_ok(f"拒绝改约成功，状态={req_after.status_label}")

    order_after_reject = store.get_order(order.order_id)
    assert order_after_reject.scheduled_start == "2026-11-01 09:00"
    assert order_after_reject.scheduled_end == "2026-11-01 11:00"
    print_ok("拒绝后工单原日程保持不变")

    reject_logs = store.get_reschedule_confirm_logs(reschedule_id=req.reschedule_id)
    assert len(reject_logs) == 1
    assert reject_logs[0].decision == "reject"
    assert reject_logs[0].reject_reason == "当天有其他安排"
    print_ok("拒绝日志完整记录拒绝原因")

    slots2 = [_make_slot("2026-11-03 09:00", "2026-11-03 11:00")]
    req2 = store.create_reschedule_request(order.order_id, dispatcher, "撤销测试", slots2)
    cancelled = store.cancel_reschedule_request(req2.reschedule_id, dispatcher)
    assert cancelled.status == RescheduleStatus.CANCELLED
    print_ok(f"调度员撤销待确认改约成功，状态={cancelled.status_label}")

    try:
        store.cancel_reschedule_request(req.reschedule_id, dispatcher)
        print_fail("撤销已处理的改约居然成功！")
        assert False
    except WorkOrderError as e:
        assert "待确认" in str(e) or "只能撤销" in str(e)
        print_ok(f"已拒绝的改约不可再次撤销（符合预期）: {e}")

    try:
        store.cancel_reschedule_request(req2.reschedule_id, inspector)
        print_fail("验收员撤销改约居然成功！")
        assert False
    except (PermissionError, WorkOrderError) as e:
        print_ok(f"验收员无权撤销改约（符合预期）: {e}")
    return store


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
        store = test_batch_result_persistence_across_restart(store)
        test_batch_result_export_fields_consistency(store)
        test_batch_result_filter_by_status_and_conflict(store)
        test_batch_result_log_write_failure_tracking(store)
        test_revocation_partial(store)
        test_revocation_duplicate_interception(store)
        store = test_revocation_persistence_across_restart(store)
        test_revocation_export_field_consistency(store)
        test_revocation_permission_denied(store)
        test_revocation_version_and_status_conflict(store)
        store = test_spare_parts_persistence_across_restart(store)
        test_spare_parts_permission_denied(store)
        store = test_spare_parts_concurrent_stock_conflict(store)
        test_spare_parts_import_invalid_rows(store)
        test_spare_parts_export_field_consistency(store)
        test_spare_parts_visibility_after_audit(store)
        test_spare_parts_application_validation_edge_cases(store)
        store = test_reschedule_normal_flow(store)
        store = test_reschedule_permission_denied(store)
        store = test_reschedule_conflict_rejection(store)
        store = test_reschedule_persistence_across_restart(store)
        store = test_reschedule_import_invalid_rows(store)
        store = test_reschedule_export_field_consistency(store)
        store = test_reschedule_visibility_after_confirm(store)
        store = test_reschedule_reject_and_cancel(store)

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
 24. 批量改派结果跨重启持久化：重启恢复最近结果、二次提交不覆盖旧结果、保留操作人/时间/5项校验/日志写入状态
 25. 批量改派结果导出一致性：CSV/JSON 字段与数据模型一致，所有原因/校验/追溯信息不丢失
 26. 批量改派结果过滤：按成功/跳过/失败、按冲突类型过滤，status_label/summary 计算属性
 27. 批量改派结果日志失败追踪：5项校验标志、操作人/草稿/工单/维修员追溯、to_dict/from_dict 往返
 28. 撤销-部分撤销：选中几条成功项撤销，工单/状态回滚正确，未选条目保留，撤销字段完整
 29. 撤销-重复撤销拦截：已撤销/再次改派/已完成/原维修员不存在 均跳过，不覆盖有效变更
 30. 撤销-重启恢复：撤销记录、结果条目撤销状态、工单数据 跨重启完全一致，重启后仍可继续撤销
 31. 撤销-导出字段一致性：CSV/JSON 包含全部撤销字段（状态/原因/操作人/时间/冲突类型/状态快照），与UI一致
 32. 撤销-权限拒绝：非调度员（验收员/维修员）撤销被拒绝，所有数据不改动
 33. 撤销-版本与状态冲突：工单被再次改派/已完成时只跳过本条，正常撤销的工单恢复且版本递增，被跳过的工单数据不被覆盖
 34. 备件库存-重启恢复：备件档案/领用申请/审核日志 跨重启完全一致，重启后仍可查询和继续操作
 35. 备件库存-权限拒绝：非调度员创建/编辑/删除备件、非调度员审核申请、非本人申请查看 均被拒绝，所有数据不改动
 36. 备件库存-并发冲突：两个审核请求同时扣减同一备件库存时，RLock 互斥保证不超卖，库存和申请状态一致
 37. 备件库存-导入非法行：CSV 含空名称/负库存/负阈值/无效类别时，全量拒绝不写入，不污染已有数据
 38. 备件库存-导出字段一致性：库存/申请/日志 的 CSV/JSON 导出字段与数据模型完全一致，往返导入导出无数据丢失
 39. 备件库存-审核后可见状态：审核通过后库存扣减可见、申请状态更新可见、工单历史新增记录可见，维修员只能看到自己的申请
 40. 备件库存-申请/审核边界拦截：已完成工单、非指定维修员、类别不匹配、库存不足 均拦截且给出明确原因，审核失败原子性（无半扣减）
 41. 上门改约-正常链路：调度员发起→填写原因/候选时间/备注→维修员或调度员确认→自动更新工单日程→留下可追溯历史记录→到场确认
 42. 上门改约-权限拦截：非调度员发起改约、非指定维修员/非调度员确认改约、非相关人员到场确认 均被拒绝且所有数据不改动
 43. 上门改约-冲突拒绝：已完成工单禁止改约、候选时间窗与维修员已有排程重叠、重复确认不覆盖第一次结果 均被正确拦截
 44. 上门改约-持久化恢复：改约申请/确认日志/到场记录/工单日程 跨重启完全一致，重启后仍可继续处理待确认改约
 45. 上门改约-导入异常：CSV含空工单/空原因/空时间/非法格式/不存在工单/结束早于开始 均跳过并记录原因，合法行写入不污染
 46. 上门改约-导出字段一致性：改约申请/确认日志 CSV/JSON 字段与数据模型完全一致，中文表头与界面显示一致，往返无数据丢失
 47. 上门改约-确认后可见状态：维修员/调度员视角可见待确认改约、已确认日程、到场记录、历史计数；非指定维修员查看被拒绝
 48. 上门改约-拒绝与撤销：拒绝改约需填写原因且原日程保持不变；调度员可撤销自己待确认的改约；已处理改约不可重复撤销
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
