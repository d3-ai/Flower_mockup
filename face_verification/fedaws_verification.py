import argparse
import gc
import os
import random
import warnings
from typing import Callable, Dict, Optional, Tuple

import numpy as np
import torch
import wandb
from client_app.face_client import FlowerFaceRayClient
from flwr.client import Client
from flwr.common import NDArray, NDArrays, Parameters, Scalar, ndarrays_to_parameters, parameters_to_ndarrays
from flwr.server.client_manager import SimpleClientManager
from models.base_model import Net
from models.driver import test
from server_app.app import ServerConfig
from server_app.strategy.fedaws import FedAwS
from server_app.wandb_server import RayTuneServer
from simulation_app.app import start_simulation
from torch.utils.data import DataLoader
from utils.utils_dataset import configure_dataset, load_centralized_dataset
from utils.utils_model import load_arcface_model
from utils.utils_wandb import custom_wandb_init

warnings.filterwarnings("ignore")

parser = argparse.ArgumentParser("Federated face verification simulation")
parser.add_argument(
    "--dataset", type=str, required=True, choices=["CIFAR10", "CelebA"], help="FL config: dataset name"
)
parser.add_argument(
    "--target",
    type=str,
    required=True,
    help="FL config: target partitions for common dataset target attributes for celeba",
)
parser.add_argument(
    "--model", type=str, required=True, choices=["ResNet18", "GNResNet18"], help="FL config: model name"
)
parser.add_argument(
    "--pretrained",
    type=str,
    required=False,
    choices=["IMAGENET1K_V1", "CelebA", "None"],
    default=None,
    help="pretraing recipe",
)
parser.add_argument("--num_rounds", type=int, required=False, default=5, help="FL config: aggregation rounds")
parser.add_argument("--num_clients", type=int, required=False, default=4, help="FL config: number of clients")
parser.add_argument("--fraction_fit", type=float, required=False, default=1, help="FL config: client selection ratio")
parser.add_argument("--local_epochs", type=int, required=False, default=5, help="Client fit config: local epochs")
parser.add_argument("--batch_size", type=int, required=False, default=10, help="Client fit config: batchsize")
parser.add_argument(
    "--criterion",
    type=str,
    required=False,
    default="CCL",
    choices=["CCL"],
    help="Criterion of classification performance",
)
parser.add_argument("--lr", type=float, required=False, default=0.01, help="Client fit config: learning rate")
parser.add_argument("--weight_decay", type=float, required=False, default=0.0, help="Client fit config: weigh_decay")
parser.add_argument("--save_model", type=int, required=False, default=0, help="flag for model saving")
parser.add_argument("--seed", type=int, required=False, default=1234, help="Random seed")


def set_seed(seed: int):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.backends.cudnn.benchmark = False


def main():
    args = parser.parse_args()
    print(args)
    set_seed(args.seed)

    dataset_config = configure_dataset(dataset_name=args.dataset, target=args.target)

    net: Net = load_arcface_model(
        name=args.model,
        input_spec=dataset_config["input_spec"],
        out_dims=dataset_config["out_dims"],
        pretrained=args.pretrained,
    )
    init_parameters: Parameters = ndarrays_to_parameters(net.get_weights())
    init_embeddings: NDArray = net.get_weights()[-1]

    client_config = {
        "dataset_name": args.dataset,
        "input_spec": dataset_config["input_spec"],
        "out_dims": 1,
        "target_name": args.target,
        "model_name": args.model,
        "pretrained": "None",  # args.pretrained,
    }
    server_config = ServerConfig(num_rounds=args.num_rounds)

    def fit_config(server_round: int) -> Dict[str, Scalar]:
        config = {
            "local_epochs": args.local_epochs,
            "batch_size": args.batch_size,
            "lr": args.lr,
            "weight_decay": args.weight_decay,
            "criterion_name": args.criterion,
        }
        return config

    def eval_config(server_round: int) -> Dict[str, Scalar]:
        config = {
            "batch_size": args.batch_size,
        }
        return config

    def get_eval_fn(model: Net, dataset: str, target: str) -> Callable:
        testset = load_centralized_dataset(dataset_name=dataset, train=False, target=target)
        testloader = DataLoader(testset, batch_size=1000)

        def evaluate(
            server_round: int, weights: NDArrays, config: Dict[str, Scalar]
        ) -> Optional[Tuple[float, Dict[str, Scalar]]]:
            model.set_weights(weights)
            device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
            results = test(model, testloader, device=device)
            torch.cuda.empty_cache()
            gc.collect()
            return results["loss"], {"accuracy": results["acc"]}

        return evaluate

    def client_fn(cid: str) -> Client:
        return FlowerFaceRayClient(cid, client_config)

    strategy = FedAwS(
        fraction_fit=args.fraction_fit,
        fraction_evaluate=1,
        min_fit_clients=int(args.num_clients * args.fraction_fit),
        min_evaluate_clients=args.num_clients,
        min_available_clients=args.num_clients,
        evaluate_fn=get_eval_fn(net, args.dataset, args.target),
        on_fit_config_fn=fit_config,
        on_evaluate_config_fn=eval_config,
        initial_parameters=init_parameters,
        initial_embeddings=init_embeddings,
        nu=0.9,
        eta=0.01,
        lam=10,
    )
    server = RayTuneServer(
        client_manager=SimpleClientManager(),
        strategy=strategy,
    )
    client_resources = {"num_cpus": 2, "num_gpus": 1.0}
    ray_config = {"include_dashboard": False, "address": "auto"}
    params_config = {
        "batch_size": args.batch_size,
        "local_epochs": args.local_epochs,
        "criterion": args.criterion,
        "pretrained": args.pretrained,
        "target": args.target,
        "lr": args.lr,
        "weight_decay": args.weight_decay,
        "model_name": args.model,
        "seed": args.seed,
        "num_clients": args.num_clients,
        "fraction_fit": args.fraction_fit,
        "api_key_file": os.environ["WANDB_API_KEY_FILE"],
    }
    custom_wandb_init(config=params_config, project="hoge_verifications", strategy="FedAwS")
    hist, params = start_simulation(
        client_fn=client_fn,
        num_clients=args.num_clients,
        client_resources=client_resources,
        server=server,
        config=server_config,
        ray_init_args=ray_config,
        keep_initialised=True,
    )

    if args.save_model:
        weights = parameters_to_ndarrays(params)
        net.set_weights(weights)
        save_path = os.path.join(wandb.run.dir, "final_model.pth")
        torch.save(net.to("cpu").state_dict(), save_path)


if __name__ == "__main__":
    main()
