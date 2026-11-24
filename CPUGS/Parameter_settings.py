#参数设置

uncertainty_intensity = 1  # 不确定性强度系数 ε，取值 {0, 0.5, 1, 1.5}，默认1

#环境
TOTAL_WORKSTATIONS = 6  # 总工作站数量
MEMORY_CAPACITY = 10  # 总容量限制
CACHE_CAPACITY = 3  # 每个工作站Cache容量

#订单部分
num_orders = 100
standard_ID_time = 15
standard_MD_time = 10
standard_A_time = 15
job_type_weights = [1, 1, 1, 1]
d_parameters = 0.7 / TOTAL_WORKSTATIONS
d2_parameters = 0.7


LOAD_TIME = 5  # 材料加载时间
MEMORY_TO_CACHE_TIME = 5  # Memory到Cache运输时间
HARDDISK_TO_MEMORY_TIME = 10  # 从harddisk运送到memory的时间
HD_TO_CACHE_TIME = MEMORY_TO_CACHE_TIME + HARDDISK_TO_MEMORY_TIME # Hard Disk到Cache运输时间
WIP_TRANSFER_TIME = 5 # 工作站之间WIP运输时间

#CPUiS
#长程调度
normal_released_capacity = 3  # 无紧急订单时维持的released订单数量
max_released_order_capacity = 6
LS_periodic_interval = 100  # LS周期性触发间隔
#中程调度
MS_threshold = 10  # GRQ和BQ的容量阈值
MS_periodic_interval = 50  # MS周期性触发间隔
#短程调度
SS_periodic_interval = 20  # SS周期性触发间隔
#OoO

#工作窃取
WS_ALPHA = 1.0  # laxity权重
WS_BETA = 1.0   # 窃取成本权重