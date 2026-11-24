"""
中程调度（Medium-term Scheduling）
基于作业特性的suspend/resume管理
特性优先级 = laxity × (job_type占比 + (1 - 材料占比))
- 优先级越大 → 越倾向suspend
- 优先级越小（含负值）→ 越倾向resume/保留
"""

import LLF_LS5


def get_job_material(job):
    """获取作业所需的材料类型（来自P操作），无P操作返回None"""
    for op in job["operations"]:
        if op["o_type"] == "P":
            return op["material"]
    return None


def is_job_urgent(job, current_time, orders):
    """判断作业是否紧急（基于order的laxity与紧急阈值）"""
    job_id = job["job_id"]
    order_id = int(job_id.split('_')[0])
    target_order = next((o for o in orders if o["order_id"] == order_id), None)
    if target_order is None:
        return False
    laxity = LLF_LS5.calculate_order_laxity(target_order, current_time)
    remaining_time = LLF_LS5.calculate_order_remaining_time(target_order)
    urgent_threshold = remaining_time
    return laxity < urgent_threshold


def calculate_job_priority(job, all_jobs_in_pair, current_time, orders):
    """
    计算作业的特性优先级
    优先级 = laxity × (job_type占比 + (1 - 材料占比))

    :param job: 当前作业
    :param all_jobs_in_pair: 用于计算占比的作业集合（active + suspended）
    :param current_time: 当前仿真时间
    :param orders: 订单列表
    :return: 特性优先级值（越大越倾向suspend）
    """
    # 1. 找到job所属order，计算laxity
    job_id = job["job_id"]
    order_id = int(job_id.split('_')[0])
    target_order = next((o for o in orders if o["order_id"] == order_id), None)
    if target_order is None:
        return float('inf')  # 找不到order，最低优先保留→大值倾向suspend
    laxity = LLF_LS5.calculate_order_laxity(target_order, current_time)

    total = len(all_jobs_in_pair)
    if total == 0:
        return laxity  # 边界情况

    # 2. 计算job_type占比（相似性）
    job_type = job.get("job_type")
    same_type_count = sum(1 for j in all_jobs_in_pair if j.get("job_type") == job_type)
    type_ratio = same_type_count / total

    # 3. 计算材料占比（材料特殊性 = 1 - 占比）
    job_material = get_job_material(job)
    if job_material is None:
        material_ratio = 0
    else:
        same_material_count = sum(
            1 for j in all_jobs_in_pair if get_job_material(j) == job_material
        )
        material_ratio = same_material_count / total

    # 4. 计算特性优先级
    priority = laxity * (type_ratio + (1 - material_ratio))
    return priority


def mid_term_scheduling(active_queue, suspended_queue, threshold, current_time, orders):
    """
    通用的MS调度函数：处理一对(active_queue, suspended_queue)
    可用于 GRQ↔RSQ 或 BQ↔BSQ

    :param active_queue: 活跃队列（GRQ 或 BQ）
    :param suspended_queue: 挂起队列（RSQ 或 BSQ）
    :param threshold: 容量阈值
    :param current_time: 当前时间
    :param orders: 订单列表
    """
    print(f"\n  >>> MS调度: active={len(active_queue)}, suspended={len(suspended_queue)}, threshold={threshold}")

    # 第一步：强制resume紧急作业
    urgent_to_resume = [job for job in suspended_queue if is_job_urgent(job, current_time, orders)]
    for job in urgent_to_resume:
        suspended_queue.remove(job)
        active_queue.append(job)
        print(f"    [紧急resume] {job['job_id']}")

    # 第二步：阈值调整
    if len(active_queue) > threshold:
        # Suspend 超出部分（优先级最大的）
        all_jobs_pair = active_queue + suspended_queue
        # 按优先级升序排序active_queue
        active_queue.sort(
            key=lambda j: calculate_job_priority(j, all_jobs_pair, current_time, orders)
        )
        num_to_suspend = len(active_queue) - threshold
        # 末尾的num_to_suspend个（优先级最大的）suspend
        jobs_to_suspend = active_queue[-num_to_suspend:]
        for job in jobs_to_suspend:
            active_queue.remove(job)
            suspended_queue.append(job)
            print(f"    [suspend] {job['job_id']}")

    elif len(active_queue) < threshold:
        # Resume 缺少部分（优先级最小的）
        all_jobs_pair = active_queue + suspended_queue
        # 按优先级升序排序suspended_queue
        suspended_queue.sort(
            key=lambda j: calculate_job_priority(j, all_jobs_pair, current_time, orders)
        )
        num_to_resume = min(threshold - len(active_queue), len(suspended_queue))
        # 前num_to_resume个（优先级最小的）resume
        jobs_to_resume = suspended_queue[:num_to_resume]
        for job in jobs_to_resume:
            suspended_queue.remove(job)
            active_queue.append(job)
            print(f"    [resume] {job['job_id']}")

    print(f"  >>> MS完成: active={len(active_queue)}, suspended={len(suspended_queue)}")