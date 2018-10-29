from __future__ import print_function

from examples.drake.generators import RelPose, Config, Trajectory, get_stable_gen, get_grasp_gen, get_ik_fn, \
    get_free_motion_fn, get_holding_motion_fn
from examples.drake.iiwa_utils import get_close_wsg50_positions, get_open_wsg50_positions, \
    open_wsg50_gripper
from examples.drake.motion import get_distance_fn, get_extend_fn, waypoints_from_path
from examples.drake.problems import load_tables

user_input = raw_input

import time
import numpy as np
import argparse

from pydrake.geometry import (ConnectDrakeVisualizer, DispatchLoadMessage)
from pydrake.lcm import DrakeLcm # Required else "ConnectDrakeVisualizer(): incompatible function arguments."
from pydrake.systems.framework import DiagramBuilder
from pydrake.systems.primitives import SignalLogger
from pydrake.trajectories import PiecewisePolynomial
from pydrake.systems.analysis import Simulator

from pddlstream.algorithms.focused import solve_focused
from pddlstream.language.generator import from_gen_fn, from_fn
from pddlstream.utils import print_solution, read, INF, get_file_path

from examples.drake.utils import get_model_joints, get_world_pose, set_world_pose, set_joint_position, \
    prune_fixed_joints, get_configuration, get_model_name

from examples.drake.kuka_multibody_controllers import (KukaMultibodyController, HandController, ManipStateMachine)

# https://drake.mit.edu/doxygen_cxx/classdrake_1_1multibody_1_1_multibody_tree.html
# wget -q https://registry.hub.docker.com/v1/repositories/mit6881/drake-course/tags -O -  | sed -e 's/[][]//g' -e 's/"//g' -e 's/ //g' | tr '}' '\n'  | awk -F: '{print $3}'
# https://stackoverflow.com/questions/28320134/how-to-list-all-tags-for-a-docker-image-on-a-remote-registry
# docker rmi $(docker images -q mit6881/drake-course)

# https://github.com/RobotLocomotion/drake/blob/a54513f9d0e746a810da15b5b63b097b917845f0/bindings/pydrake/multibody/test/multibody_tree_test.py
# ~/Programs/LIS/git/pddlstream$ ~/Programs/Classes/6811/drake_docker_utility_scripts/docker_run_bash_mac.sh drake-20181012 .
# http://127.0.0.1:7000/static/

# gz sdf -p ../urdf/iiwa14_polytope_collision.urdf > /iiwa14_polytope_collision.sdf

##################################################


def load_meshcat():
    import meshcat
    vis = meshcat.Visualizer()
    #print(dir(vis)) # set_object
    return vis


def add_meshcat_visualizer(scene_graph, builder):
    # https://github.com/rdeits/meshcat-python
    # https://github.com/RussTedrake/underactuated/blob/master/src/underactuated/meshcat_visualizer.py
    from underactuated.meshcat_visualizer import MeshcatVisualizer
    viz = MeshcatVisualizer(scene_graph)
    builder.AddSystem(viz)
    builder.Connect(scene_graph.get_pose_bundle_output_port(),
                    viz.get_input_port(0))
    viz.load()
    return viz


def add_drake_visualizer(scene_graph, lcm, builder):
    ConnectDrakeVisualizer(builder=builder, scene_graph=scene_graph, lcm=lcm)
    DispatchLoadMessage(scene_graph, lcm) # TODO: only update viewer after a plan is found


def add_logger(mbp, builder):
    state_log = builder.AddSystem(SignalLogger(mbp.get_continuous_state_output_port().size()))
    state_log._DeclarePeriodicPublish(0.02)
    builder.Connect(mbp.get_continuous_state_output_port(), state_log.get_input_port(0))
    return state_log


def connect_collisions(mbp, scene_graph, builder):
    # Connect scene_graph to MBP for collision detection.
    builder.Connect(
        mbp.get_geometry_poses_output_port(),
        scene_graph.get_source_pose_port(mbp.get_source_id()))
    builder.Connect(
        scene_graph.get_query_output_port(),
        mbp.get_geometry_query_input_port())


def connect_controllers(builder, mbp, robot, gripper, print_period=1.0):
    iiwa_controller = KukaMultibodyController(plant=mbp,
                                              kuka_model_instance=robot,
                                              print_period=print_period)
    builder.AddSystem(iiwa_controller)
    builder.Connect(iiwa_controller.get_output_port(0),
                    mbp.get_input_port(0))
    builder.Connect(mbp.get_continuous_state_output_port(),
                    iiwa_controller.robot_state_input_port)

    hand_controller = HandController(plant=mbp,
                                     model_instance=gripper)
    builder.AddSystem(hand_controller)
    builder.Connect(hand_controller.get_output_port(0),
                    mbp.get_input_port(1))
    builder.Connect(mbp.get_continuous_state_output_port(),
                    hand_controller.robot_state_input_port)

    state_machine = ManipStateMachine(mbp)
    builder.AddSystem(state_machine)
    builder.Connect(mbp.get_continuous_state_output_port(),
                    state_machine.robot_state_input_port)
    builder.Connect(state_machine.kuka_plan_output_port,
                    iiwa_controller.plan_input_port)
    builder.Connect(state_machine.hand_setpoint_output_port,
                    hand_controller.setpoint_input_port)
    return state_machine


def build_diagram(mbp, scene_graph, lcm, meshcat=False):
    builder = DiagramBuilder()
    builder.AddSystem(scene_graph)
    builder.AddSystem(mbp)
    connect_collisions(mbp, scene_graph, builder)
    if meshcat:
        add_meshcat_visualizer(scene_graph, builder)
    else:
        add_drake_visualizer(scene_graph, lcm, builder)
    add_logger(mbp, builder)
    #return builder.Build()
    return builder

##################################################

def get_pddlstream_problem(mbp, context, scene_graph, task):
    domain_pddl = read(get_file_path(__file__, 'domain.pddl'))
    stream_pddl = read(get_file_path(__file__, 'stream.pddl'))
    constant_map = {}

    robot = task.robot
    gripper = task.gripper

    #world = mbp.world_body()
    world = mbp.world_frame()
    robot_joints = prune_fixed_joints(get_model_joints(mbp, robot))
    conf = Config(robot_joints, get_configuration(mbp, context, robot))
    init = [
        ('CanMove',),
        ('Conf', conf),
        ('AtConf', conf),
        ('HandEmpty',)
    ]

    for obj in task.movable:
        obj_name = get_model_name(mbp, obj)
        #obj_frame = get_base_body(mbp, obj).body_frame()
        obj_pose = RelPose(mbp, world, obj, get_world_pose(mbp, context, obj))
        init += [('Graspable', obj_name),
                 ('Pose', obj_name, obj_pose),
                 ('AtPose', obj_name, obj_pose)]
        for surface, body_name in task.surfaces:
            surface_name = get_model_name(mbp, surface)
            init += [('Stackable', obj_name, surface_name, body_name)]
            #if is_placement(body, surface):
            #    init += [('Supported', body, pose, surface)]

    for surface, body_name in task.surfaces:
        surface_name = get_model_name(mbp, surface)
        if 'sink' in surface_name:
            init += [('Sink', surface_name)] # body_name?
        if 'stove' in surface_name:
            init += [('Stove', surface_name)]

    obj_name = get_model_name(mbp, task.movable[0])
    goal = ('and',
            ('AtConf', conf),
            #('Holding', obj_name),
            #('On', obj_name, fixed[1]),
            #('On', obj_name, fixed[2]),
            #('Cleaned', obj_name),
            ('Cooked', obj_name),
    )

    stream_map = {
        'sample-pose': from_gen_fn(get_stable_gen(task, context)),
        'sample-grasp': from_gen_fn(get_grasp_gen(task)),
        'inverse-kinematics': from_fn(get_ik_fn(mbp, context, robot, gripper, fixed=task.fixed)),
        'plan-free-motion': from_fn(get_free_motion_fn(mbp, context, robot, gripper, fixed=task.fixed)),
        'plan-holding-motion': from_fn(get_holding_motion_fn(mbp, context, robot, gripper, fixed=task.fixed)),
        #'TrajCollision': get_movable_collision_test(),
    }
    #stream_map = 'debug'

    return domain_pddl, constant_map, stream_pddl, stream_map, init, goal

##################################################


def postprocess_plan(mbp, gripper, plan):
    trajectories = []
    if plan is None:
        return trajectories

    gripper_joints = prune_fixed_joints(get_model_joints(mbp, gripper)) 
    gripper_extend_fn = get_extend_fn(gripper_joints)
    gripper_closed_conf = get_close_wsg50_positions(mbp, gripper)
    gripper_path = list(gripper_extend_fn(gripper_closed_conf, get_open_wsg50_positions(mbp, gripper)))
    gripper_path.insert(0, gripper_closed_conf)
    open_traj = Trajectory(Config(gripper_joints, q) for q in gripper_path)
    close_traj = Trajectory(reversed(open_traj.path))
    # TODO: ceiling & orientation constraints
    # TODO: sampler chooses configurations that are far apart

    for name, args in plan:
        if name in ['clean', 'cook']:
            continue
        if name == 'pick':
            o, p, g, q, t = args
            trajectories.extend([
                Trajectory(reversed(t.path)),
                close_traj,
                Trajectory(t.path, attachments=[g]),
            ])
        elif name == 'place':
            o, p, g, q, t = args
            trajectories.extend([
                Trajectory(reversed(t.path), attachments=[g]),
                open_traj,
                Trajectory(t.path),
            ])
        else:
            trajectories.append(args[-1])

    return trajectories

def step_trajectories(diagram, diagram_context, context, trajectories, time_step=0.01):
    diagram.Publish(diagram_context)
    user_input('Start?')
    for traj in trajectories:
        for _ in traj.iterate(context):
            diagram.Publish(diagram_context)
            if time_step is None:
                user_input('Continue?')
            else:
                time.sleep(time_step)
    user_input('Finish?')

def simulate_splines(diagram, diagram_context, sim_duration, real_time_rate=1.0):
    simulator = Simulator(diagram, diagram_context)
    simulator.set_publish_every_time_step(False)
    simulator.set_target_realtime_rate(real_time_rate)
    simulator.Initialize()

    diagram.Publish(diagram_context)
    user_input('Start?')
    simulator.StepTo(sim_duration)
    user_input('Finish?')

##################################################


def compute_duration(splines):
    sim_duration = 0.
    for spline in splines:
        sim_duration += spline.end_time() + 0.5
    sim_duration += 5.0
    return sim_duration


RADIANS_PER_SECOND = np.pi / 2

def convert_splines(mbp, robot, gripper, context, trajectories):
    # TODO: move to trajectory class
    print()
    splines, gripper_setpoints = [], []
    for i, traj in enumerate(trajectories):
        traj.path[-1].assign(context)
        joints = traj.path[0].joints
        if len(joints) == 2:
            q_knots_kuka = np.zeros((2, 7))
            q_knots_kuka[0] = get_configuration(mbp, context, robot) # Second is velocity
            splines.append(PiecewisePolynomial.ZeroOrderHold([0, 1], q_knots_kuka.T))
        elif len(joints) == 7:
            # TODO: adjust timing based on distance & velocities
            # TODO: adjust number of waypoints
            distance_fn = get_distance_fn(joints)
            #path = [traj.path[0].positions, traj.path[-1].positions]
            path = [q.positions for q in traj.path]
            path = waypoints_from_path(joints, path) # TODO: increase time for pick/place & hold
            q_knots_kuka = np.vstack(path).T
            distances = [0.] + [distance_fn(q1, q2) for q1, q2 in zip(path, path[1:])]
            t_knots = np.cumsum(distances) / RADIANS_PER_SECOND # TODO: this should be a max
            d, n = q_knots_kuka.shape
            print('{}) d={}, n={}, duration={:.3f}'.format(i, d, n, t_knots[-1]))
            splines.append(PiecewisePolynomial.Cubic(
                breaks=t_knots, 
                knots=q_knots_kuka,
                knot_dot_start=np.zeros(d), 
                knot_dot_end=np.zeros(d)))
        else:
            raise ValueError(joints)
        _, gripper_setpoint = get_configuration(mbp, context, gripper)
        gripper_setpoints.append(gripper_setpoint)
    return splines, gripper_setpoints


##################################################

def main():
    # TODO: GeometryInstance, InternalGeometry, & GeometryContext to get the shape of objects
    # TODO: cost-sensitive planning to avoid large kuka moves

    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--cfree', action='store_true', help='Disables collisions')
    parser.add_argument('-m', '--meshcat', action='store_true', help='Use the meshcat viewer')
    parser.add_argument('-s', '--simulate', action='store_true', help='Simulate')
    args = parser.parse_args()

    meshcat_vis = None
    if args.meshcat:
        meshcat_vis = load_meshcat()  # Important that variable is saved
        # http://127.0.0.1:7000/static/

    time_step = 0.0002 # TODO: context.get_continuous_state_vector() fails
    mbp, scene_graph, task = load_tables(time_step=time_step)
    #mbp, scene_graph, task = load_manipulation(time_step=time_step)
    print(task)

    #station, mbp, scene_graph = load_station(time_step=time_step)
    #builder.AddSystem(station)

    #dump_plant(mbp)
    #dump_models(mbp)

    ##################################################

    robot = task.robot
    gripper = task.gripper

    lcm = DrakeLcm()
    #lcm = None
    builder = build_diagram(mbp, scene_graph, lcm, args.meshcat)
    state_machine = connect_controllers(builder, mbp, robot, gripper)
    diagram = builder.Build()
    diagram_context = diagram.CreateDefaultContext()
    context = diagram.GetMutableSubsystemContext(mbp, diagram_context)
    #context = mbp.CreateDefaultContext()

    for joint, position in task.initial_positions.items():
        set_joint_position(joint, context, position)
    for model, pose in task.initial_poses.items():
        set_world_pose(mbp, context, model, pose)
    open_wsg50_gripper(mbp, context, gripper)
    #close_wsg50_gripper(mbp, context, gripper)
    #set_configuration(mbp, context, gripper, [-0.05, 0.05])

    diagram.Publish(diagram_context)
    #initial_state = context.get_continuous_state_vector().get_value() # CopyToVector
    initial_state = mbp.tree().get_multibody_state_vector(context).copy()

    ##################################################

    if args.cfree:
        task.fixed = []
    problem = get_pddlstream_problem(mbp, context, scene_graph, task)
    solution = solve_focused(problem, planner='ff-astar', max_cost=INF)
    print_solution(solution)
    plan, cost, evaluations = solution
    if plan is None:
        return
    trajectories = postprocess_plan(mbp, gripper, plan)
    splines, gripper_setpoints = convert_splines(mbp, robot, gripper, context, trajectories)
    sim_duration = compute_duration(splines)
    print('Splines: {}\nDuration: {:.3f} seconds'.format(len(splines), sim_duration))

    ##################################################

    #context.get_mutable_continuous_state_vector().SetFromVector(initial_state)
    mbp.tree().get_mutable_multibody_state_vector(context)[:] = initial_state
    #if not args.simulate:
    #    fix_input_ports(mbp, context)
    #sub_context = diagram.GetMutableSubsystemContext(mbp, diagram_context)
    #print(context == sub_context) # True

    if args.simulate:
        state_machine.Load(splines, gripper_setpoints)
        simulate_splines(diagram, diagram_context, sim_duration)
    else:
        step_trajectories(diagram, diagram_context, context, trajectories)


if __name__ == '__main__':
    main()

# python2 -m examples.drake.run
