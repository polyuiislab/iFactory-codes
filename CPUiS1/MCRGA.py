import json
import random
import numpy as np
from deap import base, creator, tools, algorithms
from copy import deepcopy
from config import MACHINE_TIME_FACTORS, calculate_worst_case_time, ready_queue_lim

print = lambda *args, **kwargs: None
pop_size1=10 * ready_queue_lim

class CacheManager:
    def __init__(self):
        self.time_cache = {}
        self.orders_cache = {}
        self.due_times_cache = {}

cache_manager = CacheManager()

if hasattr(creator, "FitnessMin"):
    del creator.FitnessMin
if hasattr(creator, "FitnessMulti"):
    del creator.FitnessMulti
if hasattr(creator, "Individual"):
    del creator.Individual

creator.create("FitnessMulti", base.Fitness, weights=(-1.0, -1.0))
creator.create("Individual", list, fitness=creator.FitnessMulti)


def clear_cache():
    global cache_manager
    cache_manager.time_cache = {}
    cache_manager.orders_cache = {}
    cache_manager.due_times_cache = {}
    return True

class MachineState:
    def __init__(self):
        self.machines = {
            1: {'current_color': 1, 'remaining_time': 0},
            2: {'current_color': 2, 'remaining_time': 0},
            3: {'current_color': 3, 'remaining_time': 0}
        }
        self.memory = {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1}
        self.machine_status = {
            1: {'status': 'normal', 'recovery_time': 0},
            2: {'status': 'normal', 'recovery_time': 0},
            3: {'status': 'normal', 'recovery_time': 0}
        }

    def get_available_machines(self):
        return [mid for mid in [1, 2, 3] if self.machine_status[mid]['status'] != 'failure']

    def change_material(self, machine_id, required_color):
        current_color = self.machines[machine_id]['current_color']
        if current_color == required_color:
            return 0, current_color
        if self.memory.get(required_color, 0) > 0:
            worst_switch_time = 10 * 1.1
            self.memory[current_color] += 1
            self.memory[required_color] -= 1
            self.machines[machine_id]['current_color'] = required_color
            return worst_switch_time, required_color
        else:
            worst_switch_time = 25 * 1.1
            self.machines[machine_id]['current_color'] = required_color
            return worst_switch_time, required_color

    def get_material(self, machine_id, required_color):
        current_color = self.machines[machine_id]['current_color']
        if current_color == required_color:
            return 0, current_color
        if self.memory.get(required_color, 0) > 0:
            worst_switch_time = 10 * 1.1
            self.memory[required_color] -= 1
            self.machines[machine_id]['current_color'] = required_color
            for i in range(1, 6):
                if self.memory[i] == 0:
                    self.memory[i] += 1
                    break
            return worst_switch_time, required_color
        else:
            worst_switch_time = 25 * 1.1
            self.machines[machine_id]['current_color'] = required_color
            return worst_switch_time, required_color


def get_machine_time_info(orders_file, job_id, order_id, machine_id):
    cache_key = f"{order_id}_{job_id}_{machine_id}"
    if cache_key in cache_manager.time_cache:
        return cache_manager.time_cache[cache_key]
    if orders_file not in cache_manager.orders_cache:
        with open(orders_file, 'r') as f:
            orders = json.load(f)
        cache_manager.orders_cache[orders_file] = orders
        for order in orders:
            order_id_temp = order['order_id']
            for job in order['jobs']:
                job_id_temp = job['job_id']
                for m_id in [1, 2, 3]:
                    key = f"{order_id_temp}_{job_id_temp}_{m_id}"
                    cache_manager.time_cache[key] = job['machine_times'][str(m_id)]['estimated_time']
    if cache_key in cache_manager.time_cache:
        return cache_manager.time_cache[cache_key]
    orders = cache_manager.orders_cache[orders_file]
    for order in orders:
        if order['order_id'] == order_id:
            for job in order['jobs']:
                if job['job_id'] == job_id:
                    result = job['machine_times'][str(machine_id)]['estimated_time']
                    cache_manager.time_cache[cache_key] = result
                    return result
    raise ValueError(f"未找到订单{order_id}的组件{job_id}")


def create_individual(ready_queue, machine_state):
    available_machines = machine_state.get_available_machines()
    if not available_machines:
        return []
    num_tasks = len(ready_queue)
    if num_tasks == 0:
        return []
    critical_tasks = []
    non_critical_tasks = []
    for idx, task in enumerate(ready_queue):
        if task.get('is_critical', 0) == 1:
            critical_tasks.append(idx)
        else:
            non_critical_tasks.append(idx)
    random.shuffle(critical_tasks)
    random.shuffle(non_critical_tasks)
    critical_machine_assignments = [(task_idx, random.choice(available_machines)) for task_idx in critical_tasks]
    non_critical_machine_assignments = [(task_idx, random.choice(available_machines)) for task_idx in
                                        non_critical_tasks]
    critical_job = group_tasks_by_color_and_machine(critical_machine_assignments, ready_queue)
    non_critical_job = group_tasks_by_color_and_machine(non_critical_machine_assignments, ready_queue)
    individual = critical_job + non_critical_job
    return individual


def group_tasks_by_color_and_machine(task_assignments, ready_queue):
    grouped_tasks = {}
    for task_idx, machine_id in task_assignments:
        color = ready_queue[task_idx]['color']
        key = (machine_id, color)
        if key not in grouped_tasks:
            grouped_tasks[key] = []
        grouped_tasks[key].append((task_idx, machine_id))
    result = []
    for tasks in grouped_tasks.values():
        result.extend(tasks)
    return result


def evaluate_multi_objective(individual, ready_queue, orders_file, machine_state):
    if not individual:
        return float('inf'), float('inf')
    if orders_file not in cache_manager.due_times_cache:
        with open(orders_file, 'r') as f:
            orders_data = json.load(f)
        cache_manager.due_times_cache[orders_file] = {order['order_id']: order['actual_due_time'] for order in orders_data}
    order_due_times = cache_manager.due_times_cache[orders_file]
    eval_machine_state = MachineState()
    for m_id in [1, 2, 3]:
        eval_machine_state.machines[m_id]['current_color'] = machine_state.machines[m_id]['current_color']
        eval_machine_state.machines[m_id]['remaining_time'] = machine_state.machines[m_id]['remaining_time']
        eval_machine_state.machine_status[m_id]['status'] = machine_state.machine_status[m_id]['status']
    for color in machine_state.memory:
        eval_machine_state.memory[color] = machine_state.memory[color]
    available_machines = eval_machine_state.get_available_machines()
    machines = {m_id: [] for m_id in available_machines}
    for task_idx, machine_id in individual:
        if machine_id not in available_machines or task_idx >= len(ready_queue):
            continue
        task = ready_queue[task_idx]
        estimated_time = get_machine_time_info(orders_file, task['job_id'], task['order_id'], machine_id)
        base_time = calculate_worst_case_time(estimated_time, machine_id)
        machines[machine_id].append({
            'task_data': task,
            'base_time': base_time,
            'is_critical': task.get('is_critical', 0)
        })
    makespan = 0
    total_setup = 0
    total_flow = 0
    total_tardiness = 0
    for mid in available_machines:
        if not machines.get(mid, []):
            continue
        current_time = eval_machine_state.machines[mid]['remaining_time']
        last_color = eval_machine_state.machines[mid]['current_color']
        for task_idx, task_info in enumerate(machines[mid]):
            task = task_info['task_data']
            required_color = task['color']
            base_time = task_info['base_time']
            arrival_time = task['arrival_time']
            worst_switch_time, new_color = eval_machine_state.change_material(mid, required_color)
            if last_color != new_color:
                base_time += worst_switch_time
                total_setup += worst_switch_time
            start_time = max(current_time, arrival_time)
            end_time = start_time + base_time
            flow_time = end_time - arrival_time
            total_flow += flow_time
            actual_due_time = order_due_times.get(task['order_id'], float('inf'))
            task_tardiness = max(0, end_time - actual_due_time)
            total_tardiness += task_tardiness
            current_time = end_time
            last_color = new_color
        makespan = max(makespan, current_time)
    return total_setup,makespan


def order_crossover(ind1, ind2, machine_state):
    size = min(len(ind1), len(ind2))
    if size <= 2:
        return creator.Individual(ind1), creator.Individual(ind2)
    ready_queue = None
    if hasattr(toolbox, 'evaluate') and hasattr(toolbox.evaluate, 'keywords'):
        ready_queue = toolbox.evaluate.keywords.get('ready_queue', None)
    if not ready_queue:
        return creator.Individual(ind1), creator.Individual(ind2)
    ind1_critical, ind1_non_critical = split_critical_non_critical(ind1, ready_queue)
    ind2_critical, ind2_non_critical = split_critical_non_critical(ind2, ready_queue)
    child1_critical, child2_critical = crossover_job(ind1_critical, ind2_critical, machine_state)
    child1_non_critical, child2_non_critical = crossover_job(ind1_non_critical, ind2_non_critical, machine_state)
    child1 = child1_critical + child1_non_critical
    child2 = child2_critical + child2_non_critical
    child1 = regroup_individual_by_criticality(child1, ready_queue)
    child2 = regroup_individual_by_criticality(child2, ready_queue)
    return creator.Individual(child1), creator.Individual(child2)


def crossover_job(job1, job2, machine_state):
    child1 = []
    child2 = []
    size = min(len(job1), len(job2))
    if size <= 2:
        return job1, job2
    cxpoint1 = random.randint(1, size - 2) if size > 3 else 1
    cxpoint2 = random.randint(cxpoint1, size - 1) if size > 3 else size - 1
    tasks1 = [gene[0] for gene in job1]
    tasks2 = [gene[0] for gene in job2]
    machine_map1 = {gene[0]: gene[1] for gene in job1}
    machine_map2 = {gene[0]: gene[1] for gene in job2}
    mid_segment1 = job1[cxpoint1:cxpoint2 + 1]
    child1.extend(mid_segment1)
    remaining_tasks = [t for t in tasks2 if t not in [gene[0] for gene in mid_segment1]]
    for task in remaining_tasks:
        machine_id = machine_map1.get(task, random.choice(machine_state.get_available_machines()))
        child1.append((task, machine_id))
    mid_segment2 = job2[cxpoint1:cxpoint2 + 1]
    child2.extend(mid_segment2)
    remaining_tasks = [t for t in tasks1 if t not in [gene[0] for gene in mid_segment2]]
    for task in remaining_tasks:
        machine_id = machine_map2.get(task, random.choice(machine_state.get_available_machines()))
        child2.append((task, machine_id))
    return child1, child2


def split_critical_non_critical(individual, ready_queue):
    critical_job = []
    non_critical_job = []
    for task_idx, machine_id in individual:
        if ready_queue[task_idx].get('is_critical', 0) == 1:
            critical_job.append((task_idx, machine_id))
        else:
            non_critical_job.append((task_idx, machine_id))
    return critical_job, non_critical_job


def regroup_individual_by_criticality(individual, ready_queue):
    critical_job, non_critical_job = split_critical_non_critical(individual, ready_queue)
    critical_job = group_tasks_by_color_and_machine(critical_job, ready_queue)
    non_critical_job = group_tasks_by_color_and_machine(non_critical_job, ready_queue)
    return critical_job + non_critical_job


def task_based_mutation(ind, machine_state):
    if len(ind) <= 1:
        return ind,
    available_machines = machine_state.get_available_machines()
    if not available_machines:
        return ind,
    ready_queue = None
    if hasattr(toolbox, 'evaluate') and hasattr(toolbox.evaluate, 'keywords'):
        ready_queue = toolbox.evaluate.keywords.get('ready_queue', None)
    if not ready_queue:
        return ind,
    mutant = deepcopy(ind)
    critical_job, non_critical_job = split_critical_non_critical(mutant, ready_queue)
    if random.random() < 0.5:
        if random.random() < 0.5 and critical_job:
            critical_job = mutate_task_group_order(critical_job, ready_queue)
        elif non_critical_job:
            non_critical_job = mutate_task_group_order(non_critical_job, ready_queue)
    else:
        if random.random() < 0.5 and critical_job:
            critical_job = mutate_task_machine(critical_job, ready_queue, available_machines)
        elif non_critical_job:
            non_critical_job = mutate_task_machine(non_critical_job, ready_queue, available_machines)
    mutant = critical_job + non_critical_job
    return creator.Individual(mutant),


def mutate_task_group_order(task_job, ready_queue):
    task_groups = identify_task_groups(task_job, ready_queue)
    if len(task_groups) < 2:
        return task_job
    group_indices = random.sample(range(len(task_groups)), 2)
    idx1, idx2 = min(group_indices), max(group_indices)
    start1, end1 = task_groups[idx1]
    start2, end2 = task_groups[idx2]
    part1 = task_job[:start1]
    group1 = task_job[start1:end1 + 1]
    part2 = task_job[end1 + 1:start2]
    group2 = task_job[start2:end2 + 1]
    part3 = task_job[end2 + 1:]
    new_task_job = part1 + group2 + part2 + group1 + part3
    return new_task_job


def mutate_task_machine(task_job, ready_queue, available_machines):
    if not task_job or not available_machines:
        return task_job
    task_pos = random.randint(0, len(task_job) - 1)
    task_idx, old_machine = task_job[task_pos]
    new_machines = [m for m in available_machines if m != old_machine]
    if not new_machines:
        return task_job
    new_machine = random.choice(new_machines)
    task_job[task_pos] = (task_idx, new_machine)
    return group_tasks_by_color_and_machine(task_job, ready_queue)


def identify_task_groups(task_job, ready_queue):
    if not task_job:
        return []
    task_attributes = []
    for i, (task_idx, machine_id) in enumerate(task_job):
        color = ready_queue[task_idx]['color']
        task_attributes.append((i, machine_id, color))
    groups = []
    current_machine = None
    current_color = None
    start_idx = None
    for i, (idx, machine_id, color) in enumerate(task_attributes):
        if machine_id != current_machine or color != current_color:
            if start_idx is not None:
                groups.append((start_idx, i - 1))
            start_idx = i
            current_machine = machine_id
            current_color = color
    if start_idx is not None:
        groups.append((start_idx, len(task_attributes) - 1))
    return groups


def setup_robust_ga(ready_queue, orders_file, machine_state=MachineState()):
    global toolbox
    toolbox = base.Toolbox()
    toolbox.register("individual", tools.initIterate, creator.Individual,
                     lambda: create_individual(ready_queue, machine_state))
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)
    toolbox.register("mate", lambda ind1, ind2: order_crossover(ind1, ind2, machine_state))
    toolbox.register("mutate", lambda ind: task_based_mutation(ind, machine_state))
    toolbox.register("select", tools.selNSGA2)
    toolbox.register("evaluate", evaluate_multi_objective,
                     ready_queue=ready_queue,
                     orders_file=orders_file,
                     machine_state=machine_state)
    return toolbox


def run_robust_optimization(toolbox, pop_size=pop_size1, n_gen=20, cx_prob=0.7, mut_prob=0.3):
    pop = toolbox.population(n=pop_size)
    stats = tools.Statistics(lambda ind: ind.fitness.values)
    stats.register("min", np.min, axis=0)
    stats.register("avg", np.mean, axis=0)
    hof = tools.ParetoFront()
    logbook = tools.Logbook()
    logbook.header = ['gen', 'evals'] + stats.fields
    invalid_ind = [ind for ind in pop if not ind.fitness.valid]
    fitnesses = toolbox.map(toolbox.evaluate, invalid_ind)
    for ind, fit in zip(invalid_ind, fitnesses):
        ind.fitness.values = fit
    hof.update(pop)
    record = stats.compile(pop)
    logbook.record(gen=0, evals=len(invalid_ind), **record)
    for gen in range(1, n_gen + 1):
        offspring = toolbox.select(pop, len(pop))
        offspring = list(map(toolbox.clone, offspring))
        for i in range(1, len(offspring), 2):
            if i < len(offspring) - 1:
                if random.random() < cx_prob:
                    offspring[i - 1], offspring[i] = toolbox.mate(offspring[i - 1], offspring[i])
                    del offspring[i - 1].fitness.values
                    del offspring[i].fitness.values
        for i in range(len(offspring)):
            if random.random() < mut_prob:
                offspring[i], = toolbox.mutate(offspring[i])
                del offspring[i].fitness.values
        invalid_ind = [ind for ind in offspring if not ind.fitness.valid]
        fitnesses = toolbox.map(toolbox.evaluate, invalid_ind)
        for ind, fit in zip(invalid_ind, fitnesses):
            ind.fitness.values = fit
        pop[:] = offspring
        hof.update(pop)
        record = stats.compile(pop)
        logbook.record(gen=gen, evals=len(invalid_ind), **record)
    if hof:
        sorted_hof = sorted(hof, key=lambda x: x.fitness.values[0])
        best_makespan_ind = sorted_hof[0]
        return hof, logbook, best_makespan_ind
    return hof, logbook, None


def validate_chromosome(ind, machine_state):
    if len(ind) < 2:
        return False
    available_machines = machine_state.get_available_machines()
    for _, machine_id in ind:
        if machine_id not in available_machines:
            return False
    task_indices = [task_idx for task_idx, _ in ind]
    if len(task_indices) != len(set(task_indices)):
        return False
    return True