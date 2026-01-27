import numpy as np
import json
from copy import deepcopy
from config import AST, MACHINE_TIME_FACTORS, calculate_worst_case_time


print = lambda *args, **kwargs: None


def calculate_job_avg_processing_time(job):
    machine_times = job['machine_times']
    estimated_times = [v['estimated_time'] for v in machine_times.values()]
    return np.mean(estimated_times)


def check_material_availability(job, machine_states, memory):
    required_color = job['color']
    machine_colors = set()
    for machine_id, machine_info in machine_states.items():
        machine_colors.add(machine_info['current_color'])
    memory_colors = set()
    for color, available in memory.items():
        if available > 0:
            memory_colors.add(color)
    return required_color in machine_colors or required_color in memory_colors


def check_urgent_orders(released_orders, current_time = 0):
    urgent_orders = []
    for order in released_orders:
        worst_EPi = 0.0
        for job in order['jobs']:
            machine_times = job['machine_times']
            job_avg_worst_time = np.mean([
                calculate_worst_case_time(v['estimated_time'], machine_id)
                for machine_id, v in machine_times.items()
            ])
            worst_EPi += job_avg_worst_time
        n_i = len(order['jobs'])
        actual_due_time = order.get('actual_due_time', float('inf'))
        JIT_i = (actual_due_time - current_time) - (worst_EPi + AST * n_i)
        if JIT_i < 0:
            urgent_orders.append(order)
    return urgent_orders


def get_order_jobs_from_queue(queue, order_id):
    return [job for job in queue if job['order_id'] == order_id]


def remove_order_jobs_from_queue(queue, order_id):
    jobs_to_remove = []
    remaining_jobs = []
    for job in queue:
        if job['order_id'] == order_id:
            jobs_to_remove.append(job)
        else:
            remaining_jobs.append(job)
    queue[:] = remaining_jobs
    return jobs_to_remove


def sort_queue_by_processing_time(queue):
    return sorted(queue, key = lambda job: calculate_job_avg_processing_time(job))


def insert_jobs_sorted(queue, jobs):
    for job in jobs:
        job_processing_time = calculate_job_avg_processing_time(job)
        insert_pos = 0
        for i, existing_job in enumerate(queue):
            if calculate_job_avg_processing_time(existing_job) > job_processing_time:
                insert_pos = i
                break
            insert_pos = i + 1
        queue.insert(insert_pos, job)


def midterm_scheduling(ready_queue, ready_suspend_queue, released_orders,
                       machine_states, memory, ready_queue_lim, current_time = 0):
    urgent_orders = check_urgent_orders(released_orders, current_time)
    if urgent_orders:
        for urgent_order in urgent_orders:
            urgent_jobs = remove_order_jobs_from_queue(ready_suspend_queue, urgent_order['order_id'])
            ready_queue.extend(urgent_jobs)
        urgent_order_ids = {order['order_id'] for order in urgent_orders}
        non_urgent_jobs = []
        urgent_jobs_in_rq = []
        for job in ready_queue:
            if job['order_id'] in urgent_order_ids:
                urgent_jobs_in_rq.append(job)
            else:
                non_urgent_jobs.append(job)
        ready_queue[:] = urgent_jobs_in_rq
        ready_suspend_queue.extend(non_urgent_jobs)
    available_jobs = []
    unavailable_jobs = []
    for job in ready_queue:
        if check_material_availability(job, machine_states, memory):
            available_jobs.append(job)
        else:
            unavailable_jobs.append(job)
    ready_queue[:] = available_jobs
    ready_suspend_queue.extend(unavailable_jobs)
    if (not urgent_orders) or len(ready_queue) == 0:
        available_jobs_from_rsq = []
        remaining_jobs_in_rsq = []
        for job in ready_suspend_queue:
            if check_material_availability(job, machine_states, memory):
                available_jobs_from_rsq.append(job)
            else:
                remaining_jobs_in_rsq.append(job)
        ready_suspend_queue[:] = remaining_jobs_in_rsq
        ready_queue.extend(available_jobs_from_rsq)
    ready_queue[:] = sort_queue_by_processing_time(ready_queue)
    ready_suspend_queue[:] = sort_queue_by_processing_time(ready_suspend_queue)
    if len(ready_queue) > ready_queue_lim:
        jobs_to_keep = ready_queue[:ready_queue_lim]
        jobs_to_move = ready_queue[ready_queue_lim:]
        ready_queue[:] = jobs_to_keep
        insert_jobs_sorted(ready_suspend_queue, jobs_to_move)
    return ready_queue, ready_suspend_queue