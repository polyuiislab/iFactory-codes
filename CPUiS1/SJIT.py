import numpy as np
import math
import json
from config import M, AST, MACHINE_TIME_FACTORS, calculate_worst_case_time


print = lambda *args, **kwargs: None


def calculate_sjit_priority(order, CS_ref, current_time=0):
    order_id = order['order_id']
    jobs = order['jobs']
    actual_due_time = order.get('actual_due_time', float('inf'))
    order_colors = set()
    for job in jobs:
        order_colors.add(job['color'])
    CS_i = order_colors
    worst_EPi = 0.0
    for job in jobs:
        machine_times = job['machine_times']
        job_avg_worst_time = np.mean([
            calculate_worst_case_time(v['estimated_time'], machine_id)
            for machine_id, v in machine_times.items()
        ])
        worst_EPi += job_avg_worst_time
    n_i = len(jobs)
    intersection = CS_i.intersection(CS_ref)
    union = CS_i.union(CS_ref)
    SIM_i = len(intersection) / len(union) if len(union) > 0 else 0
    JIT_i = abs(worst_EPi + AST * n_i - (actual_due_time - current_time))
    SJIT_i = (M - SIM_i) * JIT_i
    return {
        'order_id': order_id,
        'SJIT_priority': SJIT_i,
        'SIM_i': SIM_i,
        'JIT_i': JIT_i,
        'worst_EPi': worst_EPi,
        'n_i': n_i,
        'CS_i': CS_i,
        'actual_due_time': actual_due_time
    }


def sort_orders_by_priority(unreleased_orders, CS_ref, current_time=0):
    arrived_orders = [order for order in unreleased_orders if order['arrival_time'] <= current_time]
    order_priorities = []
    for order in arrived_orders:
        priority_info = calculate_sjit_priority(order, CS_ref, current_time)
        order_with_priority = order.copy()
        order_with_priority.update(priority_info)
        order_priorities.append(order_with_priority)
    return sorted(order_priorities, key=lambda x: x['SJIT_priority'])


def update_ready_queue(sorted_orders, ready_queue, all_tasks, current_time=0):
    if not sorted_orders:
        return None
    selected_order = sorted_orders[0]
    for job in selected_order['jobs']:
        matching_task = None
        for task in all_tasks:
            if (task['order_id'] == selected_order['order_id'] and
                    task['job_id'] == job['job_id']):
                matching_task = task.copy()
                break
        if matching_task:
            ready_queue.append(matching_task)
        else:
            job_task = {
                'task_id': len(all_tasks) + len(ready_queue) + 1,  # 临时生成ID
                'order_id': selected_order['order_id'],
                'job_id': job['job_id'],
                'color': job['color'],
                'arrival_time': selected_order['arrival_time'],
                'due_time': selected_order['due_time'],
                'actual_due_time': selected_order['actual_due_time'],
                'machine_times': job['machine_times'],
                'status': 0,
                'actual_duration': 0.0,
                'uncertainty_type': job.get('uncertainty_type', 0),
                'uncertainty_details': job.get('uncertainty_details', {}),
                'processed_time': 0.0,
                'cumulative_setup_time': 0.0
            }
            ready_queue.append(job_task)
    sorted_orders.remove(selected_order)
    print(f"订单 {selected_order['order_id']} 已释放，包含 {len(selected_order['jobs'])} 个job")
    print("ready_queue:", ready_queue)
    return selected_order