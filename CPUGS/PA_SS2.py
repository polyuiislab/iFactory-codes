def get_job_material(job):
    """
    获取作业所需的材料类型
    返回: material类型(1-10), 如果没有P类型operation返回None
    """
    for operation in job['operations']:
        if operation['o_type'] == 'P':
            return operation['material']
    return None


def calculate_affinity(workstation, job_material):
    """
    计算工作站对某个作业的亲和性
    参数:
        workstation: 工作站对象
        job_material: 作业所需的材料类型
    返回:
        亲和性值 (0到1之间的浮点数)
    """
    # 如果工作站队列为空，且cache中有所需材料，返回0.99，以保证如果有类似任务已经分配到另一个空工作站，该任务没跟着去
    if len(workstation.job_queue) == 0:
        return 0.99

    # 计算队列中有多少作业需要相同材料
    same_material_count = 0
    for queued_job in workstation.job_queue:
        queued_material = get_job_material(queued_job)
        if queued_material == job_material:
            same_material_count += 1

    # 亲和性 = 相同材料任务数 / 总任务数
    affinity = same_material_count / len(workstation.job_queue)
    return affinity

def get_required_resources(job):
    """
    获取作业所需的资源类型集合（用于资源可用性判断）
    返回: set，包含 'id_resource', 'md_resource', 'printer_3d', 'assembly_resource' 中需要的元素
    """
    required = set()
    for op in job["operations"]:
        if op["o_type"] == "D":
            required.add("id_resource")
            required.add("md_resource")
        elif op["o_type"] == "P":
            required.add("printer_3d")
        elif op["o_type"] == "A":
            required.add("assembly_resource")
    return required


def affinity_based_scheduling(global_ready_queue, workstations, unavailable_resources=None):
    """
    基于处理器亲和性的短程调度算法（含EU资源可用性判断）

    参数:
        global_ready_queue: 全局就绪队列
        workstations: 工作站列表
        unavailable_resources: {ws_id: set(resource_types)}，因EU不可用的资源
    返回:
        (modified_ws_ids, pending_jobs)
        - modified_ws_ids: 被分配作业的工作站ID列表
        - pending_jobs: 因cache无材料而延迟分配的作业列表（需触发物流）
    """
    if unavailable_resources is None:
        unavailable_resources = {}

    modified_ws_ids = []
    pending_jobs = []  # 因cache无材料延迟的作业

    for job in global_ready_queue.copy():
        job_material = get_job_material(job)
        required_resources = get_required_resources(job)

        # 先看哪些ws cache有所需材料
        ws_with_material = [ws for ws in workstations
                            if ws.cache_area.get(job_material, 0) > 0]

        # 再筛选资源可用的
        eligible_workstations = []
        for ws in ws_with_material:
            unavailable = unavailable_resources.get(ws.ws_id, set())
            if not (required_resources & unavailable):
                eligible_workstations.append(ws)

        # 区分两种延迟原因
        if not eligible_workstations:
            if not ws_with_material:
                # 仅因cache无材料 → 加入pending_jobs等待物流触发
                pending_jobs.append(job)
                print(f"延迟分配（cache无材料）: 作业 {job['job_id']} 需要材料{job_material}")
            else:
                # 因EU资源占用 → 不触发物流，等EU结束
                print(f"延迟分配（资源被EU占用）: 作业 {job['job_id']}")
            continue

        # 正常分配（亲和性逻辑）
        workstation_scores = []
        for ws in eligible_workstations:
            affinity = calculate_affinity(ws, job_material)
            queue_length = len(ws.job_queue)
            workstation_scores.append({
                'workstation': ws,
                'affinity': affinity,
                'queue_length': queue_length
            })
        workstation_scores.sort(key=lambda x: (-x['affinity'], x['queue_length']))
        best_workstation = workstation_scores[0]['workstation']
        best_workstation.job_queue.append(job)
        modified_ws_ids.append(best_workstation.ws_id)

        print(f"作业 {job['job_id']} (材料{job_material}) 分配到 {best_workstation.ws_id}")
        global_ready_queue.remove(job)

    return modified_ws_ids, pending_jobs

# region test
# WorkStation类定义（用于测试）
MATERIAL_TYPES = list(range(1, 11))  # 材料类型1-10


class WorkStation:
    def __init__(self, ws_id, printer_type):
        self.ws_id = ws_id
        self.job_queue = []
        self.id_designer = 1
        self.scanner_3d = 1
        self.md_designer = 1
        self.computer = 1
        self.printer_3d_type = printer_type
        self.printer_3d = 1
        self.printer_material = 0
        self.cache_area = {material_type: 0 for material_type in MATERIAL_TYPES}
        self.cache_capacity = 3
        self.temp_wip_storage = []
        self.assembly_station = 1
        self.assembly_worker = 1


# 测试示例
if __name__ == '__main__':
    # 创建6个工作站
    TOTAL_WORKSTATIONS = 6
    workstations = []
    printer_types = [1, 1, 2, 2, 3, 3]

    for i in range(TOTAL_WORKSTATIONS):
        ws_id = f"WS-{i + 1:02d}"
        printer_type = printer_types[i]
        station = WorkStation(ws_id, printer_type)

        # 初始化cache区物料
        mapping = {
            0: [1, 2, 3],
            1: [4, 5, 6],
            2: [7, 8, 9],
            3: [10, 1, 2],
            4: [3, 4, 5],
            5: [6, 7, 8],
        }
        for idx in mapping.get(i, []):
            station.cache_area[idx] = 1
        workstations.append(station)

    # 创建测试用的global_ready_queue
    global_ready_queue = [
        {
            'job_id': 'order1_job1',
            'job_type': 1,
            'operations': [
                {'o_id': 'order1_job1_D', 'o_type': 'D'},
                {'o_id': 'order1_job1_P', 'o_type': 'P', 'material': 2},
                {'o_id': 'order1_job1_A', 'o_type': 'A'}
            ]
        },
        {
            'job_id': 'order1_job2',
            'job_type': 4,
            'operations': [
                {'o_id': 'order1_job2_P', 'o_type': 'P', 'material': 5}
            ]
        },
        {
            'job_id': 'order2_job1',
            'job_type': 3,
            'operations': [
                {'o_id': 'order2_job1_P', 'o_type': 'P', 'material': 2},
                {'o_id': 'order2_job1_A', 'o_type': 'A'}
            ]
        },
        {
            'job_id': 'order2_job2',
            'job_type': 4,
            'operations': [
                {'o_id': 'order2_job2_P', 'o_type': 'P', 'material': 7}
            ]
        },
        {
            'job_id': 'order3_job1',
            'job_type': 2,
            'operations': [
                {'o_id': 'order3_job1_D', 'o_type': 'D'},
                {'o_id': 'order3_job1_P', 'o_type': 'P', 'material': 3}
            ]
        }
    ]

    print("=== 调度前工作站状态 ===")
    for ws in workstations:
        cache_materials = [m for m, count in ws.cache_area.items() if count > 0]
        print(f"{ws.ws_id} (打印机类型{ws.printer_3d_type}): Cache材料 {cache_materials}, 队列长度 {len(ws.job_queue)}")

    print("\n=== 待分配作业 ===")
    for job in global_ready_queue:
        material = get_job_material(job)
        print(f"{job['job_id']}: 需要材料 {material}")

    # 调用调度函数
    print("\n=== 执行调度 ===")
    updated_workstations = affinity_based_scheduling(global_ready_queue, workstations)

    # 查看分配结果
    print("\n=== 调度后工作站状态 ===")
    for ws in updated_workstations:
        print(f"\n{ws.ws_id}: {len(ws.job_queue)} 个作业")
        for job in ws.job_queue:
            material = get_job_material(job)
            print(f"  - {job['job_id']}, 材料: {material}")

    # 验证统计
    print("\n=== 调度统计 ===")
    total_assigned = sum(len(ws.job_queue) for ws in workstations)
    print(f"总作业数: {len(global_ready_queue)}")
    print(f"已分配作业数: {total_assigned}")
    print(f"分配成功率: {total_assigned}/{len(global_ready_queue)}")
# endregion