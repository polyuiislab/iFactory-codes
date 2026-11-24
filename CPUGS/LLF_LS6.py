"""
LLF (Least Laxity First) Scheduler - 最小松弛度优先调度
订单级别的调度算法

松弛度定义：
订单松弛度 = 截止时间 - (当前时间 + 订单剩余处理时间)
松弛度越小，订单越紧急
"""

import copy


def calculate_order_remaining_time(order):
    """
    计算订单的剩余处理时间（按工序完成状态扣减，仅累加尚未完成的部分）
    参数:
        order: 订单对象
    返回:
        float: 订单剩余处理时间
    计算规则（仅统计未完成部分）：
    - D阶段: 未完成ID累加standard_ID_time，未完成MD累加standard_MD_time
             （D为两段式ID->MD，依据ID_completed/MD_completed分别判断）
    - P阶段: 未完成则累加机器1的estimated_time
    - A阶段: 未完成则累加standard_A_time
    """
    total_time = 0.0
    # 遍历订单中的所有job
    for job in order["jobs"]:
        # 遍历job中的所有operation
        for operation in job["operations"]:
            o_type = operation["o_type"]
            if o_type == "D":
                # D阶段为两段式（ID->MD），按各自完成标志分别扣减
                if not operation.get("ID_completed", False):
                    total_time += operation["standard_ID_time"]
                if not operation.get("MD_completed", False):
                    total_time += operation["standard_MD_time"]
            elif o_type == "P":
                # P阶段：未完成才计入
                if operation["status"] != "completed":
                    machine_times = operation["machine_times"]
                    estimated_time = machine_times["1"]["estimated_time"]
                    total_time += estimated_time
            elif o_type == "A":
                # A阶段：未完成才计入
                if operation["status"] != "completed":
                    total_time += operation["standard_A_time"]
    return round(total_time, 2)


def calculate_order_laxity(order, current_time):
    """
    计算订单的松弛度
    参数:
        order: 订单对象
        current_time: 当前时间
    返回:
        float: 订单松弛度
    松弛度 = 截止时间 - (当前时间 + 订单剩余处理时间 / 工作站数量)
    - 正值：表示还有缓冲时间
    - 负值：表示已经或即将逾期
    - 松弛度越小越紧急
    """
    due_time = order["due_time"]
    remaining_time = calculate_order_remaining_time(order)
    laxity = due_time - (current_time + remaining_time)  #不考虑并行能力做标准缓冲
    return round(laxity, 2)


def llf_schedule(current_time, unreleased_orders,normal_released_capacity, max_released_capacity, released_orders, global_ready_queues,
                 order_release_times):
    """
    LLF调度主函数：根据松弛度选择要release的订单，带门控机制
    """
    # 步骤1: 计算当前released订单数量
    current_released_count = len(released_orders)

    # 如果已经达到最大容量，直接返回
    if current_released_count >= max_released_capacity:
        return unreleased_orders, released_orders

    # 如果没有未释放订单，直接返回
    if not unreleased_orders:
        return unreleased_orders, released_orders

    # 计算所有未释放订单的松弛度和剩余时间
    orders_with_laxity = []
    for order in unreleased_orders:
        laxity = calculate_order_laxity(order, current_time)
        remaining_time = calculate_order_remaining_time(order)
        urgent_threshold = remaining_time
        is_urgent = laxity < urgent_threshold

        orders_with_laxity.append({
            "order": order,
            "laxity": laxity,
            "remaining_time": remaining_time,
            "urgent_threshold": urgent_threshold,
            "is_urgent": is_urgent
        })

    # 按松弛度升序排序（松弛度最小的排在前面，最紧急）
    orders_with_laxity.sort(key=lambda x: x["laxity"])

    # ====================【修改开始：紧急优先，普通订单补足常规容量】====================
    # 修改：先在最大容量内选取紧急订单，再用普通订单补足 normal_released_capacity。
    # 目的：避免仅有少量紧急订单时释放数量低于常规目标；普通订单不会填满最大容量。
    available_capacity = max_released_capacity - current_released_count

    urgent_items = [item for item in orders_with_laxity if item["is_urgent"]]
    selected_urgent_items = urgent_items[:available_capacity]

    normal_slots = min(
        max(
            0,
            normal_released_capacity
            - current_released_count
            - len(selected_urgent_items)
        ),
        available_capacity - len(selected_urgent_items)
    )

    normal_items = [
        item for item in orders_with_laxity
        if not item["is_urgent"]
    ]
    selected_normal_items = normal_items[:normal_slots]

    orders_to_release = [
        item["order"]
        for item in selected_urgent_items + selected_normal_items
    ]

    print(
        f"  本次释放：紧急订单 {len(selected_urgent_items)} 个，"
        f"普通订单 {len(selected_normal_items)} 个；"
        f"释放后总数 {current_released_count + len(orders_to_release)}，"
        f"常规容量 {normal_released_capacity}，"
        f"最大容量 {max_released_capacity}"
    )
    # ====================【修改结束：紧急优先，普通订单补足常规容量】====================

    # 更新订单状态并移动到released_orders
    for order in orders_to_release:
        # 更新状态为released
        order["status"] = "released"
        # 记录order的release时间
        order_release_times[order["order_id"]] = current_time

        print(f"  Release Order {order['order_id']} @ 时间 {current_time}")

        # 将该订单的所有job加入global_ready_queues
        for job in order["jobs"]:
            # 更新job状态为ready
            job["status"] = "ready"
            # 将job的第一个operation设置为ready，其他保持unready
            if len(job["operations"]) > 0:
                job["operations"][0]["status"] = "ready"
            # 将job加入global_ready_queues
            global_ready_queues.append(job)

        unreleased_orders.remove(order)
        # 添加到released_orders
        released_orders.append(order)

    return unreleased_orders, released_orders


if __name__ == "__main__":
    """
    测试代码：演示LLF调度算法的使用
    """
    import json
    import sys
    # 检查是否提供了订单文件路径
    if len(sys.argv) > 1:
        order_file = sys.argv[1]
    else:
        order_file = "new_orders.json"
    print("=" * 60)
    print("LLF (Least Laxity First) 调度算法测试")
    print("=" * 60)
    try:
        # 读取订单数据
        with open(order_file, 'r', encoding = 'utf-8') as f:
            data = json.load(f)
        # 提取订单列表
        if "orders" in data:
            all_orders = data["orders"]
        else:
            all_orders = data
        # 为所有订单初始化status（如果没有的话）
        for order in all_orders:
            if "status" not in order:
                order["status"] = "unreleased"
            for job in order["jobs"]:
                if "status" not in job:
                    job["status"] = "unready"
                for operation in job["operations"]:
                    if "status" not in operation:
                        operation["status"] = "unready"
        print(f"\n已加载 {len(all_orders)} 个订单")
        # 分类订单
        unreleased_orders = [o for o in all_orders if o["status"] == "unreleased"]
        released_orders = [o for o in all_orders if o["status"] == "released"]
        print(f"未释放订单数: {len(unreleased_orders)}")
        print(f"已释放订单数: {len(released_orders)}")
        # 测试场景
        current_time = 0  # 当前时间为0
        max_capacity = 3  # 系统最多允许3个released订单
        print(f"\n测试场景:")
        print(f"  当前时间: {current_time}")
        print(f"  系统最大容量: {max_capacity}")
        print(f"  当前已释放订单数: {len(released_orders)}")
        print(f"  可用容量: {max_capacity - len(released_orders)}")
        # 显示unreleased订单的松弛度信息
        print(f"\n未释放订单的松弛度分析:")
        print(f"{'订单ID':<10} {'到达时间':<12} {'截止时间':<12} {'剩余处理时间':<15} {'松弛度':<10}")
        print("-" * 70)
        arrived_unreleased = [o for o in unreleased_orders if o["arrival_time"] <= current_time]
        for order in arrived_unreleased:
            remaining = calculate_order_remaining_time(order)
            laxity = calculate_order_laxity(order, current_time)
            print(f"{order['order_id']:<10} {order['arrival_time']:<12} {order['due_time']:<12} {remaining:<15.2f} {laxity:<10.2f}")
        # 执行LLF调度
        print(f"\n执行LLF调度...")
        updated_unreleased, updated_released = llf_schedule(
            current_time,
            unreleased_orders,
            max_capacity,
            released_orders
        )
        # 显示调度结果
        print(f"\n调度结果:")
        print(f"  更新后未释放订单数: {len(updated_unreleased)}")
        print(f"  更新后已释放订单数: {len(updated_released)}")
        print(f"  本次释放订单数: {len(updated_released) - len(released_orders)}")
        if len(updated_released) > len(released_orders):
            print(f"\n本次释放的订单:")
            newly_released = [o for o in updated_released if o not in released_orders]
            for order in newly_released:
                laxity = calculate_order_laxity(order, current_time)
                print(f"  订单ID: {order['order_id']}, 松弛度: {laxity:.2f}")
        print("\n" + "=" * 60)
        print("测试完成")
        print("=" * 60)
    except FileNotFoundError:
        print(f"\n错误: 找不到文件 '{order_file}'")
        print("请确保订单文件存在，或者使用以下命令生成订单:")
        print("  python order_generate1.py")
    except Exception as e:
        print(f"\n错误: {e}")
        import traceback
        traceback.print_exc()