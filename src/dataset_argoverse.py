import copy
import math
import multiprocessing
import os
import pickle
import random
from statistics import mean
import zlib
from collections import defaultdict
from typing import Dict, List, Optional
from multiprocessing import Process
from random import choice

import numpy as np
import torch
from argoverse.map_representation.map_api import ArgoverseMap
from tqdm import tqdm

import utils_cython
import utils
from utils import get_name, get_file_name_int, get_angle, logging, rotate, round_value, get_pad_vector, get_dis, get_subdivide_polygons
from utils import get_points_remove_repeated, get_one_subdivide_polygon, get_dis_point_2_polygons, larger, equal, assert_
from utils import get_neighbour_points, get_subdivide_points, get_unit_vector, get_dis_point_2_points

TIMESTAMP = 0
TRACK_ID = 1
OBJECT_TYPE = 2
X = 3
Y = 4
CITY_NAME = 5

type2index = {}
type2index["OTHERS"] = 0
type2index["AGENT"] = 1
type2index["AV"] = 2

max_vector_num = 0

VECTOR_PRE_X = 0
VECTOR_PRE_Y = 1
VECTOR_X = 2
VECTOR_Y = 3 


def get_sub_map(args: utils.Args, x, y, city_name, vectors=[], polyline_spans=[], mapping=None):
    """
    Calculate lanes which are close to (x, y) on map.

    Only take lanes which are no more than args.max_distance away from (x, y).

    """

    if args.not_use_api:
        pass
    else:
        assert isinstance(am, ArgoverseMap)
        # Add more lane attributes, such as 'has_traffic_control', 'is_intersection' etc.
        if 'semantic_lane' in args.other_params:
            lane_ids = am.get_lane_ids_in_xy_bbox(x, y, city_name, query_search_range_manhattan=args.max_distance)
            # Mask out lanes (polygons) with a p% probability
            if 'mask_lanes' in args.other_params:
                lane_ids = [lane for lane in lane_ids if random.random() > float(args.other_params['p'])]
            local_lane_centerlines = [am.get_lane_segment_centerline(lane_id, city_name) for lane_id in lane_ids]
            polygons = local_lane_centerlines

            if args.visualize:
                angle = mapping['angle']
                vis_lanes = [am.get_lane_segment_polygon(lane_id, city_name)[:, :2] for lane_id in lane_ids]
                t = []
                for each in vis_lanes:
                    for point in each:
                        point[0], point[1] = rotate(point[0] - x, point[1] - y, angle)
                    num = len(each) // 2
                    t.append(each[:num].copy())
                    t.append(each[num:num * 2].copy())
                vis_lanes = t
                mapping['vis_lanes'] = vis_lanes
        else:
            polygons = am.find_local_lane_centerlines(x, y, city_name,
                                                      query_search_range_manhattan=args.max_distance)
        polygons = [polygon[:, :2].copy() for polygon in polygons]
        angle = mapping['angle']
        for index_polygon, polygon in enumerate(polygons):
            for i, point in enumerate(polygon):
                point[0], point[1] = rotate(point[0] - x, point[1] - y, angle)
                if 'scale' in mapping:
                    assert 'enhance_rep_4' in args.other_params
                    scale = mapping['scale']
                    point[0] *= scale
                    point[1] *= scale


        def dis_2(point):
            return point[0] * point[0] + point[1] * point[1]

        def get_dis(point_a, point_b):
            return np.sqrt((point_a[0] - point_b[0]) ** 2 + (point_a[1] - point_b[1]) ** 2)

        def get_dis_for_points(point, polygon):
            dis = np.min(np.square(polygon[:, 0] - point[0]) + np.square(polygon[:, 1] - point[1]))
            return np.sqrt(dis)

        def ok_dis_between_points(points, points_, limit):
            dis = np.inf
            for point in points:
                dis = np.fmin(dis, get_dis_for_points(point, points_))
                if dis < limit:
                    return True
            return False

        def get_hash(point):
            return round((point[0] + 500) * 100) * 1000000 + round((point[1] + 500) * 100)

        lane_idx_2_polygon_idx = {}
        for polygon_idx, lane_idx in enumerate(lane_ids):
            lane_idx_2_polygon_idx[lane_idx] = polygon_idx

        # There is a lane scoring module (see Section 3.2) in the paper in order to reduce the number of goal candidates.
        # In this implementation, we use goal scoring instead of lane scoring, because we observed that it performs slightly better than lane scoring.
        # Here we only sample sparse goals, and dense goal sampling is performed after goal scoring (see decoder).
        if 'goals_2D' in args.other_params:
            points = []
            visit = {}
            point_idx_2_unit_vector = []

            mapping['polygons'] = polygons

            # Retrieve all points in the polygones (lanes on a radius of 50m) - goals_2D
            for index_polygon, polygon in enumerate(polygons): 
                for i, point in enumerate(polygon):
                    hash = get_hash(point)  # create hash table for points 
                    if hash not in visit:
                        visit[hash] = True
                        points.append(point)

                # Subdivide lanes to get more fine-grained 2D goals.
                if 'subdivide' in args.other_params: 
                    subdivide_points = get_subdivide_points(polygon)  
                    points.extend(subdivide_points)
                    subdivide_points = get_subdivide_points(polygon, include_self=True)  

            mapping['goals_2D'] = np.array(points) 

        # Create vectors for polygones/lanes
        for index_polygon, polygon in enumerate(polygons): 
            assert_(2 <= len(polygon) <= 10, info=len(polygon))  #most of the lengths are 10 points
            # assert len(polygon) % 2 == 1
            # if args.visualize:
            #     traj = np.zeros((len(polygon), 2))
            #     for i, point in enumerate(polygon):
            #         traj[i, 0], traj[i, 1] = point[0], point[1]
            #     mapping['trajs'].append(traj)

            start = len(vectors)
            if 'semantic_lane' in args.other_params:
                assert len(lane_ids) == len(polygons)
                lane_id = lane_ids[index_polygon]
                lane_segment = am.city_lane_centerlines_dict[city_name][lane_id]
            assert_(len(polygon) >= 2)
            for i, point in enumerate(polygon):
                if i > 0:
                    vector = [0] * args.hidden_size
                    vector[-1 - VECTOR_PRE_X], vector[-1 - VECTOR_PRE_Y] = point_pre[0], point_pre[1]
                    vector[-1 - VECTOR_X], vector[-1 - VECTOR_Y] = point[0], point[1]
                    vector[-5] = 1
                    vector[-6] = i #position in the polyline

                    vector[-7] = len(polyline_spans)

                    if 'semantic_lane' in args.other_params:
                        vector[-8] = 1 if lane_segment.has_traffic_control else -1
                        vector[-9] = 1 if lane_segment.turn_direction == 'RIGHT' else \
                            -1 if lane_segment.turn_direction == 'LEFT' else 0
                        vector[-10] = 1 if lane_segment.is_intersection else -1
                    point_pre_pre = (2 * point_pre[0] - point[0], 2 * point_pre[1] - point[1]) 
                    if i >= 2:
                        point_pre_pre = polygon[i - 2]
                    vector[-17] = point_pre_pre[0]
                    vector[-18] = point_pre_pre[1]

                    vectors.append(vector)
                point_pre = point

            end = len(vectors)
            if start < end:
                polyline_spans.append([start, end])

    return (vectors, polyline_spans)


def preprocess_map(map_dict):
    """
    Preprocess map to calculate potential polylines.
    """

    for city_name in map_dict:
        ways = map_dict[city_name]['way']
        nodes = map_dict[city_name]['node']
        polylines = []
        polylines_dict = {}
        for way in ways:
            polyline = []
            points = way['nd']
            points = [nodes[int(point['@ref'])] for point in points]
            point_pre = None
            for i, point in enumerate(points):
                if i > 0:
                    vector = [float(point_pre['@x']), float(point_pre['@y']), float(point['@x']), float(point['@y'])]
                    polyline.append(vector)
                point_pre = point

            if len(polyline) > 0:
                index_x = round_value(float(point_pre['@x']))
                index_y = round_value(float(point_pre['@y']))
                if index_x not in polylines_dict:
                    polylines_dict[index_x] = []
                polylines_dict[index_x].append(polyline)
                polylines.append(polyline)

        map_dict[city_name]['polylines'] = polylines
        map_dict[city_name]['polylines_dict'] = polylines_dict


def preprocess(args, id2info, mapping):
    """
    This function calculates matrix based on information from get_instance.
    """

    ### Get History: Agents + Map -> Vectors = Matrix ###

    polyline_spans = []
    keys = list(id2info.keys()) 
    assert 'AV' in keys
    assert 'AGENT' in keys
    keys.remove('AV')
    keys.remove('AGENT')
    keys = ['AGENT', 'AV'] + keys # Agent, AV, others IDs
    vectors = []
    two_seconds = mapping['two_seconds']
    mapping['trajs'] = []
    mapping['agents'] = []
    for id in keys:
        # Mask per agent with 50% probability if id != 'AV' and id != 'AGENT' 
        if 'mask_agents' in args.other_params and id != 'AV' and id != 'AGENT' and random.random() < float(args.other_params['p']):
            continue 

        info = id2info[id]
        if 'mask_agents_frames' in args.other_params and id != 'AV' and id != 'AGENT':
            info = [ i for i in info if random.random() > float(args.other_params['p']) ]

        start = len(vectors)
        if args.no_agents:
            if id != 'AV' and id != 'AGENT':
                break

        agent = [] # x, y 
        for i, line in enumerate(info):
            if larger(line[TIMESTAMP], two_seconds):   
                break # outside of history, either future anns, or other agents don't appear in the history.
            agent.append((line[X], line[Y]))

        if args.visualize:
            traj = np.zeros([args.hidden_size])
            for i, line in enumerate(info):
                if larger(line[TIMESTAMP], two_seconds):
                    traj = traj[:i * 2].copy()
                    break
                traj[i * 2], traj[i * 2 + 1] = line[X], line[Y]
                if i == len(info) - 1:
                    traj = traj[:(i + 1) * 2].copy()
            traj = traj.reshape((-1, 2))
            mapping['trajs'].append(traj)

        for i, line in enumerate(info):
            if larger(line[TIMESTAMP], two_seconds):
                break # outside of history
            x, y = line[X], line[Y]
            if i > 0:
                # print(x-line_pre[X], y-line_pre[Y])
                vector = [line_pre[X], line_pre[Y], x, y, line[TIMESTAMP], line[OBJECT_TYPE] == 'AV',
                          line[OBJECT_TYPE] == 'AGENT', line[OBJECT_TYPE] == 'OTHERS', len(polyline_spans), i]
                vectors.append(get_pad_vector(vector))
            line_pre = line

        end = len(vectors)
        if end - start == 0:
            assert id != 'AV' and id != 'AGENT'
        else:
            mapping['agents'].append(np.array(agent))

            polyline_spans.append([start, end])

    assert_(len(mapping['agents']) == len(polyline_spans))

    assert len(vectors) <= max_vector_num

    t = len(vectors)
    mapping['map_start_polyline_idx'] = len(polyline_spans)
    if args.use_map:
        vectors, polyline_spans = get_sub_map(args, mapping['cent_x'], mapping['cent_y'], mapping['city_name'],
                                              vectors=vectors,
                                              polyline_spans=polyline_spans, mapping=mapping)

    # logging('len(vectors)', t, len(vectors), prob=0.01)

    matrix = np.array(vectors)
    # matrix = np.array(vectors, dtype=float)
    # del vectors

    # matrix = torch.zeros([len(vectors), args.hidden_size])
    # for i, vector in enumerate(vectors):
    #     for j, each in enumerate(vector):
    #         matrix[i][j].fill_(each)


    ### Get Labels ###

    labels = []
    info = id2info['AGENT']
    info = info[mapping['agent_pred_index']:]
    if not args.do_test:
        if 'set_predict' in args.other_params:
            pass
        else:
            assert len(info) == 30
    for line in info:
        labels.append(line[X])
        labels.append(line[Y])

    if 'set_predict' in args.other_params:
        if 'test' in args.data_dir[0]:
            labels = [0.0 for _ in range(60)]

    if 'goals_2D' in args.other_params:
        point_label = np.array(labels[-2:])
        mapping['goals_2D_labels'] = np.argmin(get_dis(mapping['goals_2D'], point_label)) # select the closest goal
        
        if 'lane_scoring' in args.other_params:
            stage_one_label = 0
            polygons = mapping['polygons']
            min_dis = 10000.0
            for i, polygon in enumerate(polygons):
                temp = np.min(get_dis(polygon, point_label))
                if temp < min_dis:
                    min_dis = temp
                    stage_one_label = i

            mapping['stage_one_label'] = stage_one_label # select the polygon with the closest point

    mapping.update(dict(
        matrix=matrix,
        labels=np.array(labels).reshape([30, 2]),
        polyline_spans=[slice(each[0], each[1]) for each in polyline_spans],
        labels_is_valid=np.ones(args.future_frame_num, dtype=np.int64),
        eval_time=30,
    ))

    return mapping


def argoverse_get_instance(lines, file_name, args):
    """
    Extract polylines from one example file content.
    """

    global max_vector_num
    vector_num = 0
    id2info = {}
    mapping = {}
    mapping['file_name'] = file_name

    for i, line in enumerate(lines):

        line = line.strip().split(',')
        if i == 0:
            mapping['start_time'] = float(line[TIMESTAMP])
            mapping['city_name'] = line[CITY_NAME]

        line[TIMESTAMP] = float(line[TIMESTAMP]) - mapping['start_time']
        line[X] = float(line[X])
        line[Y] = float(line[Y])
        id = line[TRACK_ID]

        if line[OBJECT_TYPE] == 'AV' or line[OBJECT_TYPE] == 'AGENT':
            line[TRACK_ID] = line[OBJECT_TYPE]

        if line[TRACK_ID] in id2info:
            id2info[line[TRACK_ID]].append(line)
            vector_num += 1
        else:
            id2info[line[TRACK_ID]] = [line]

        if line[OBJECT_TYPE] == 'AGENT' and len(id2info['AGENT']) == 20:
            assert 'AV' in id2info
            assert 'cent_x' not in mapping
            agent_lines = id2info['AGENT']
            mapping['cent_x'] = agent_lines[-1][X]
            mapping['cent_y'] = agent_lines[-1][Y]
            mapping['agent_pred_index'] = len(agent_lines) # what for? it'll always be 20 (if len(agent_lines) == 20)
            mapping['two_seconds'] = line[TIMESTAMP]

            # Smooth the direction of agent. Only taking the direction of the last frame is not accurate due to label error.
            if 'direction' in args.other_params:
                span = agent_lines[-args.mode_num:]
                intervals = [2]
                angles = []
                for interval in intervals:
                    for j in range(len(span)):
                        if j + interval < len(span):
                            der_x, der_y = span[j + interval][X] - span[j][X], span[j + interval][Y] - span[j][Y]
                            angles.append([der_x, der_y])

            der_x, der_y = agent_lines[-1][X] - agent_lines[-2][X], agent_lines[-1][Y] - agent_lines[-2][Y]
    if not args.do_test:
        if 'set_predict' in args.other_params:
            pass
        else:
            assert len(id2info['AGENT']) == 50

    if vector_num > max_vector_num:
        max_vector_num = vector_num  # vector_num is the number of vectors in the sequence 
        
    if 'cent_x' not in mapping: # if there is no cent_x, then ¿it's a test file? or we don't have good annotations 
        return None

    if args.do_eval:
        origin_labels = np.zeros([30, 2])
        for i, line in enumerate(id2info['AGENT'][20:]):
            origin_labels[i][0], origin_labels[i][1] = line[X], line[Y]
        mapping['origin_labels'] = origin_labels

    angle = -get_angle(der_x, der_y) + math.radians(90)

    # Smooth the direction of agent. Only taking the direction of the last frame is not accurate due to label error.
    if 'direction' in args.other_params:
        angles = np.array(angles)
        der_x, der_y = np.mean(angles, axis=0)
        angle = -get_angle(der_x, der_y) + math.radians(90)

    mapping['angle'] = angle
    for id in id2info:
        info = id2info[id]
        for line in info:
            line[X], line[Y] = rotate(line[X] - mapping['cent_x'], line[Y] - mapping['cent_y'], angle)
        if 'scale' in mapping:
            scale = mapping['scale']
            line[X] *= scale
            line[Y] *= scale
    return preprocess(args, id2info, mapping)


class Dataset(torch.utils.data.Dataset):
    def __init__(self, args, batch_size, to_screen=True):
        data_dir = args.data_dir
        self.ex_list = []
        self.args = args

        if args.reuse_temp_file:
            pickle_file = open(os.path.join(args.temp_file_dir, get_name('ex_list')), 'rb')
            self.ex_list = pickle.load(pickle_file)
            # self.ex_list = self.ex_list[len(self.ex_list) // 2:]
            pickle_file.close()
        else:
            global am
            am = ArgoverseMap()
            if args.core_num >= 1:
                # TODO
                files = []
                for each_dir in data_dir:
                    root, dirs, cur_files = os.walk(each_dir).__next__()
                    files.extend([os.path.join(each_dir, file) for file in cur_files if
                                  file.endswith("csv") and not file.startswith('.')]) 
                if args.debug:
                    files = files[:200]

                pbar = tqdm(total=len(files))

                queue = multiprocessing.Queue(args.core_num)
                queue_res = multiprocessing.Queue()

                def calc_ex_list(queue, queue_res, args):
                    res = []
                    dis_list = []
                    while True:
                        file = queue.get()
                        if file is None:
                            break
                        if file.endswith("csv"):
                            with open(file, "r", encoding='utf-8') as fin:
                                lines = fin.readlines()[1:]
                            instance = argoverse_get_instance(lines, file, args)
                            if instance is not None:
                                data_compress = zlib.compress(pickle.dumps(instance))
                                res.append(data_compress)
                                queue_res.put(data_compress)
                            else:
                                queue_res.put(None)

                processes = [Process(target=calc_ex_list, args=(queue, queue_res, args,)) for _ in range(args.core_num)]
                for each in processes:
                    each.start()
                # res = pool.map_async(calc_ex_list, [queue for i in range(args.core_num)])
                for file in files:
                    assert file is not None
                    queue.put(file)
                    pbar.update(1)

                # necessary because queue is out-of-order
                while not queue.empty():
                    pass

                pbar.close()

                self.ex_list = []

                pbar = tqdm(total=len(files))
                for i in range(len(files)):
                    t = queue_res.get()
                    if t is not None:
                        self.ex_list.append(t)
                    pbar.update(1)
                pbar.close()
                pass

                for i in range(args.core_num):
                    queue.put(None)
                for each in processes:
                    each.join()

            else:
                assert False

            pickle_file = open(os.path.join(args.temp_file_dir, get_name('ex_list')), 'wb')
            pickle.dump(self.ex_list, pickle_file)
            pickle_file.close()
        assert len(self.ex_list) > 0
        if to_screen:
            print("valid data size is", len(self.ex_list))
            logging('max_vector_num', max_vector_num)
        self.batch_size = batch_size

    def __len__(self):
        return len(self.ex_list)

    def __getitem__(self, idx):
        # file = self.ex_list[idx]
        # pickle_file = open(file, 'rb')
        # instance = pickle.load(pickle_file)
        # pickle_file.close()

        data_compress = self.ex_list[idx]
        instance = pickle.loads(zlib.decompress(data_compress))
        return instance


def post_eval(args, file2pred, file2pred_int, file2score, file2score_int, file2labels, DEs, city_names, agent_dir_var_list, 
                agent_dir_int_var_list, opposite_dir_batch, max_guesses=None):
    from argoverse.evaluation.eval_forecasting import get_drivable_area_compliance

    score_file = args.model_recover_path.split('/')[-1]
    score_file_int = args.model_recover_path.split('/')[-1]+'_intention'
    for each in args.eval_params:
        each = str(each)
        if len(each) > 15:
            each = 'long'
        score_file += '.' + str(each)
        # if 'minFDE' in args.other_params:
        #     score_file += '.minFDE'
    if args.method_span[0] >= utils.NMS_START:
        score_file += '.NMS'
    else:
        score_file += '.score'

    for method in utils.method2FDEs:
        FDEs = utils.method2FDEs[method]
        miss_rate = np.sum(np.array(FDEs) > 2.0) / len(FDEs)
        if method >= utils.NMS_START:
            method = 'NMS=' + str(utils.NMS_LIST[method - utils.NMS_START])
        utils.logging(
            'method {}, FDE {}, MR {}, other_errors {}'.format(method, np.mean(FDEs), miss_rate, utils.other_errors_to_string()),
            type=score_file, to_screen=True, append_time=True) 
    utils.logging('other_errors {}'.format(utils.other_errors_to_string()),
                  type=score_file, to_screen=True, append_time=True)
    if max_guesses == None:
        max_guesses = 3
    if 'mask_lanes' in args.other_params: 
        utils.logging('Mask lanes with {} probability'.format(args.other_params['p']), type=score_file, to_screen=True, append_time=True)
    elif 'mask_agents' in args.other_params: 
        utils.logging('Mask agents with {} probability'.format(args.other_params['p']), type=score_file, to_screen=True, append_time=True)
    elif 'mask_agents' in args.other_params: 
        utils.logging('Mask agents frames with {} probability'.format(args.other_params['p']), type=score_file, to_screen=True, append_time=True)
        
    utils.logging('Max guesses: {}'.format(max_guesses), type=score_file, to_screen=True, append_time=True)
    
    metric_results = get_displacement_errors_and_miss_rate(file2pred, file2labels, max_guesses, 30, 2.0, file2score)
    metric_results["DAC"] = get_drivable_area_compliance(file2pred, city_names, max_guesses)
    metric_results["p-rF"] = metric_results["p_avgFDE"] / metric_results["p-minFDE"]  
    metric_results["yaw_var"] = sum(agent_dir_var_list) / len(agent_dir_var_list)
    metric_results["opposite_dir"] = opposite_dir_batch / len(agent_dir_var_list)
    utils.logging(metric_results, type=score_file, to_screen=True, append_time=True)
    if args.clustering:
        metric_results_int = get_displacement_errors_and_miss_rate(file2pred_int, file2labels, max_guesses, 30, 2.0, file2score_int)
        metric_results_int["DAC"] = get_drivable_area_compliance(file2pred_int, city_names, max_guesses)
        metric_results_int["p-rF"] = metric_results_int["p_avgFDE"] / metric_results_int["p-minFDE"]  
        metric_results_int["yaw_var"] =  sum(agent_dir_int_var_list) / len(agent_dir_int_var_list)
        utils.logging(metric_results_int, type=score_file, to_screen=True, append_time=True)

    DE = np.concatenate(DEs, axis=0)
    length = DE.shape[1]
    DE_score = [0, 0, 0, 0]
    for i in range(DE.shape[0]):
        DE_score[0] += DE[i].mean()
        for j in range(1, 4):
            index = round(float(length) * j / 3) - 1
            assert index >= 0
            DE_score[j] += DE[i][index]
    for j in range(4):
        score = DE_score[j] / DE.shape[0]
        utils.logging('ADE' if j == 0 else 'DE@1' if j == 1 else 'DE@2' if j == 2 else 'DE@3', score,
                      type=score_file, to_screen=True, append_time=True)

    utils.logging(vars(args), is_json=True,
                  type=score_file, to_screen=True, append_time=True)



def get_displacement_errors_and_miss_rate(
    forecasted_trajectories: Dict[int, List[np.ndarray]],
    gt_trajectories: Dict[int, np.ndarray],
    max_guesses: int,
    horizon: int,
    miss_threshold: float,
    forecasted_probabilities: Optional[Dict[int, List[float]]] = None,
) -> Dict[str, float]:
    from argoverse.evaluation.eval_forecasting import get_ade, get_fde
    LOW_PROB_THRESHOLD_FOR_METRICS = 0.05
    """Compute min fde and ade for each sample.

    Note: Both min_fde and min_ade values correspond to the trajectory which has minimum fde.
    The Brier Score is defined here:
        Brier, G. W. Verification of forecasts expressed in terms of probability. Monthly weather review, 1950.
        https://journals.ametsoc.org/view/journals/mwre/78/1/1520-0493_1950_078_0001_vofeit_2_0_co_2.xml

    Args:
        forecasted_trajectories: Predicted top-k trajectory dict with key as seq_id and value as list of trajectories.
                Each element of the list is of shape (pred_len x 2).
        gt_trajectories: Ground Truth Trajectory dict with key as seq_id and values as trajectory of
                shape (pred_len x 2)
        max_guesses: Number of guesses allowed
        horizon: Prediction horizon
        miss_threshold: Distance threshold for the last predicted coordinate
        forecasted_probabilities: Probabilites associated with forecasted trajectories.

    Returns:
        metric_results: Metric values for minADE, minFDE, MR, p-minADE, p-minFDE, p-MR, brier-minADE, brier-minFDE
    """
    metric_results: Dict[str, float] = {}
    min_ade, prob_min_ade, brier_min_ade = [], [], []
    min_fde, avg_fde, p_avg_fde, prob_min_fde, brier_min_fde = [], [], [], [], []
    n_misses, prob_n_misses = [], []
    for k, v in gt_trajectories.items():
        curr_min_ade = float("inf")
        curr_min_fde = float("inf")
        min_idx = 0
        max_num_traj = min(max_guesses, len(forecasted_trajectories[k]))

        # If probabilities available, use the most likely trajectories, else use the first few
        if forecasted_probabilities is not None:
            sorted_idx = np.argsort([-x for x in forecasted_probabilities[k]], kind="stable")
            # sorted_idx = np.argsort(forecasted_probabilities[k])[::-1]
            pruned_probabilities = [forecasted_probabilities[k][t] for t in sorted_idx[:max_num_traj]]
            # Normalize
            prob_sum = sum(pruned_probabilities)
            pruned_probabilities = [p / prob_sum for p in pruned_probabilities]
        else:
            sorted_idx = np.arange(len(forecasted_trajectories[k]))
        pruned_trajectories = [forecasted_trajectories[k][t] for t in sorted_idx[:max_num_traj]]

        avgfde = 0
        p_avgfde = 0
        for j in range(len(pruned_trajectories)):
            fde = get_fde(pruned_trajectories[j][:horizon], v[:horizon])
            avgfde += fde 
            p_avgfde += fde + min(
                    -np.log(pruned_probabilities[j]),
                    -np.log(LOW_PROB_THRESHOLD_FOR_METRICS),
                ) 
            if fde < curr_min_fde:
                min_idx = j
                curr_min_fde = fde
        curr_min_ade = get_ade(pruned_trajectories[min_idx][:horizon], v[:horizon])
        min_ade.append(curr_min_ade)
        min_fde.append(curr_min_fde)
        avg_fde.append(avgfde/len(pruned_trajectories))
        p_avg_fde.append(p_avgfde/len(pruned_trajectories))
        n_misses.append(curr_min_fde > miss_threshold)

        if forecasted_probabilities is not None:
            prob_n_misses.append(1.0 if curr_min_fde > miss_threshold else (1.0 - pruned_probabilities[min_idx]))
            prob_min_ade.append(
                min(
                    -np.log(pruned_probabilities[min_idx]),
                    -np.log(LOW_PROB_THRESHOLD_FOR_METRICS),
                )
                + curr_min_ade
            )
            brier_min_ade.append((1 - pruned_probabilities[min_idx]) ** 2 + curr_min_ade)
            prob_min_fde.append(
                min(
                    -np.log(pruned_probabilities[min_idx]),
                    -np.log(LOW_PROB_THRESHOLD_FOR_METRICS),
                )
                + curr_min_fde
            ) 
            brier_min_fde.append((1 - pruned_probabilities[min_idx]) ** 2 + curr_min_fde)
            brier_min_fde.append((1 - pruned_probabilities[min_idx]) ** 2 + curr_min_fde)

    metric_results["minADE"] = sum(min_ade) / len(min_ade)
    metric_results["minFDE"] = sum(min_fde) / len(min_fde)
    metric_results["avgFDE"] = sum(avg_fde) / len(avg_fde)
    metric_results["p_avgFDE"] = sum(p_avg_fde) / len(p_avg_fde)
    metric_results["MR"] = sum(n_misses) / len(n_misses)
    if forecasted_probabilities is not None:
        metric_results["p-minADE"] = sum(prob_min_ade) / len(prob_min_ade)
        metric_results["p-minFDE"] = sum(prob_min_fde) / len(prob_min_fde)
        metric_results["p-MR"] = sum(prob_n_misses) / len(prob_n_misses)
        metric_results["brier-minADE"] = sum(brier_min_ade) / len(brier_min_ade)
        metric_results["brier-minFDE"] = sum(brier_min_fde) / len(brier_min_fde)
    return metric_results
