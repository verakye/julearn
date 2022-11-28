import numpy as np
from sklearn.compose import (
    ColumnTransformer,
    TransformedTargetRegressor,
)
from sklearn.pipeline import Pipeline
from dataclasses import dataclass
from typing import Any, Union, List, Dict, Optional, Tuple

from ..transformers import (
    get_transformer,
    list_transformers,
    SetColumnTypes,
    JuTransformer,
)
from .. models import list_models, get_model, JuModel, WrapModel
from .. utils import raise_error, warn, logger, make_type_selector
from .. utils.column_types import ensure_apply_to
from .. prepare import prepare_hyperparameter_tuning


class NoInversePipeline(Pipeline):
    def inverse_transform(self, X):
        return X


def _params_to_pipeline(param, X_types):
    if isinstance(param, PipelineCreator):
        param = param.to_pipeline(X_types=X_types)
    elif isinstance(param, list):
        param = [_params_to_pipeline(_v, X_types) for _v in param]
    elif isinstance(param, dict):
        param = {
            k: _params_to_pipeline(_v, X_types) for k, _v in param.items()
        }
    elif isinstance(param, tuple):
        param = tuple(_params_to_pipeline(_v, X_types) for _v in param)
    return param


@dataclass
class Step:
    name: str
    estimator: Any
    apply_to: Any = "continuous"
    needed_types: Any = "continuous"
    params_to_tune: dict = None

    def __post_init__(self):
        self.params_to_tune = (
            {} if self.params_to_tune is None else self.params_to_tune
        )


class PipelineCreator:  # Pipeline creator
    def __init__(self):
        self._steps = list()
        self._added_target_transformer = False
        self._added_model = False

    def add(
        self,
        step,
        apply_to="continuous",
        problem_type="classification",
        **params,
    ):

        apply_to = self._ensure_apply_to(apply_to)
        self.validate_step(step, apply_to)
        name = step if isinstance(step, str) else step.__cls__.lower()
        logger.info(f"Adding step {name} that applies to {apply_to}")
        name = self._ensure_name(name)
        params_to_set = dict()
        params_to_tune = dict()
        for param, vals in params.items():
            # If we have more than 1 value, we will tune it. If not, it will
            # be set in the model.
            if hasattr(vals, "__iter__") and not isinstance(vals, str):
                if len(vals) > 1:
                    logger.info(f"Tuning hyperparameter {param} = {vals}")
                    params_to_tune[param] = vals
                else:
                    logger.info(f"Setting hyperparameter {param} = {vals[0]}")
                    params_to_set[param] = vals[0]
            else:
                logger.info(f"Setting hyperparameter {param} = {vals}")
                params_to_set[param] = vals
        estimator = (
            self.get_estimator_from(step, problem_type, **params_to_set)
            if isinstance(step, str)
            else step
        )
        if isinstance(estimator, JuTransformer):
            estimator = estimator.set_params(apply_to=apply_to)
            needed_types = estimator.get_needed_types()
        else:
            needed_types = apply_to
        if apply_to == "targt":
            name = f"target_{name}"
        self._steps.append(
            Step(
                name=name,
                estimator=estimator,
                apply_to=apply_to,
                needed_types=needed_types,
                params_to_tune=params_to_tune,
            )
        )
        logger.info("Step added")
        return self

    @property
    def steps(self):
        return self._steps

    def has_model(self) -> bool:
        return self._added_model

    @classmethod
    def from_list(cls, transformers: Union[str, list], model_params: dict):
        preprocessor = cls()
        if isinstance(transformers, str):
            transformers = [transformers]
        for transformer_name in transformers:
            t_params = {
                x.replace(f"{transformer_name}__", ""): y
                for x, y in model_params.items()
                if x.startswith(f"{transformer_name}__")
            }
            preprocessor.add(transformer_name, **t_params)
        return preprocessor

    def to_pipeline(
        self, X_types: Optional[Dict[str, List]] = None, search_params=None
    ):

        logger.debug("Creating pipeline")
        if not self.has_model():
            raise_error("Cannot create a pipeline without a model")
        pipeline_steps: List[Tuple[str, Any]] = [
            ("set_column_types", SetColumnTypes(X_types))
        ]

        X_types_patterns = self.X_types_to_patterns(X_types)

        transformer_steps = self._steps[:-1]
        model_step = self._steps[-1]
        target_transformer_steps = []

        if self._added_target_transformer:
            _transformer_steps = []
            for _step in self._steps:
                if _step.apply_to == "target":
                    target_transformer_steps.append(_step)
                else:
                    _transformer_steps.append(_step)
            transformer_steps = _transformer_steps

        # Add transformers
        wrap = X_types_patterns != ["__:type:__continuous"]
        params_to_tune = {}
        for step_dict in transformer_steps:
            logger.debug(f"Adding transformer {step_dict.name}")
            name = step_dict.name
            name_for_tuning = name
            estimator = step_dict.estimator
            logger.debug(f"\t Estimator: {estimator}")
            step_params_to_tune = step_dict.params_to_tune
            logger.debug(f"\t Params to tune: {step_params_to_tune}")

            # Wrap in a JuTransformer if needed
            if wrap and not isinstance(estimator, JuTransformer):
                estimator = self.wrap_step(name, estimator, step_dict.apply_to)
                name_for_tuning = f"wrapped_{name}__{name}"
                name = f"wrapped_{name}"

            pipeline_steps.append((name, estimator))

            # Add params to tune
            params_to_tune.update(
                {
                    f"{name_for_tuning}__{param}": val
                    for param, val in step_params_to_tune.items()
                }
            )

        model_name = model_step.name
        model_name_for_tuning = model_name
        model_estimator = model_step.estimator
        logger.debug(f"Adding model {model_name}")

        model_params = model_estimator.get_params(deep=False)
        model_params = {
            k: _params_to_pipeline(v, X_types=X_types)
            for k, v in model_params.items()
        }
        model_estimator.set_params(**model_params)
        if wrap and not isinstance(model_estimator, JuModel):

            model_name_for_tuning = f"wrapped_{model_name}__{model_name}"
            model_name = f"wrapped_{model_name}"

            logger.debug(f"Wrapping {model_name}")
            model_estimator = WrapModel(model_estimator, model_step.apply_to)

        step_params_to_tune = {
            f"{model_name_for_tuning}__{k}": v
            for k, v in model_step.params_to_tune.items()
        }

        logger.debug(f"\t Estimator: {model_estimator}")
        logger.debug("\t Looking for nested pipeline creators")
        logger.debug(f"\t Params to tune: {step_params_to_tune}")

        params_to_tune.update(step_params_to_tune)
        pipeline_steps.append((model_name, model_estimator))
        pipeline = Pipeline(pipeline_steps).set_output(transform="pandas")

        # Deal with the Hyperparameter tuning
        out = prepare_hyperparameter_tuning(
            params_to_tune, search_params, pipeline
        )
        logger.debug("Pipeline created")
        return out

    @staticmethod
    def wrap_target_model(
        model_step, target_transformer_steps, model_params=None
    ):
        model_params = {} if model_params is None else model_params
        if target_transformer_steps == []:
            return (model_step.name, model_step.estimator), model_params
        transformer_pipe = NoInversePipeline(target_transformer_steps)
        target_model = TransformedTargetRegressor(
            transformer=transformer_pipe,
            regressor=model_step[1],
            check_inverse=False,
        )
        return (f"{model_step[0]}_target_transform", target_model)

    def _validate_model_params(self, model_name, model_params):

        for param in model_params.keys():
            if "__" in param:
                est_name = param.split("__")[0]
                if est_name != model_name:
                    raise_error(
                        "Only parameters for the model should be specified. "
                        f"Got {param} for {est_name}."
                    )

    def _ensure_name(self, name):

        count = np.array([_step.name == name for _step in self._steps]).sum()
        return f"{name}_{count}" if count > 0 else name

    def validate_step(self, step, apply_to):

        if self._is_transfromer_step(step):
            if self._added_model:
                raise_error("Cannot add a transformer after adding a model")
            if self._added_target_transformer and apply_to != "target":
                raise_error(
                    "Cannot add a non-target transformer after adding "
                    "a target transformer."
                )
            if apply_to == "target":
                self._added_target_transformer = True
        elif self._is_model_step(step):
            self._added_model = True
        else:
            raise_error(f"Cannot add a {step}. I don't know what it is.")

    def X_types_to_patterns(self, X_types: Optional[Dict] = None):
        if X_types in [None, {}]:
            all_types = ["continuous"]
        else:
            all_types = list(X_types.keys())
        needed_types = []
        # steps = self._steps[:-1] if self._added_model else self._steps
        steps = self._steps
        for step_dict in steps:
            _apply_to = step_dict.apply_to
            # remove regex boilerplate
            if "|" in _apply_to:
                _needed_types = [
                    pattern.split("__:type:__")[-1]
                    for pattern in _apply_to[3:-1].split("|")

                ]
                needed_types.extend(_needed_types)

            elif "__:type:__" in _apply_to:
                # additional :-1 to get rid of the ) at the end of patterns
                needed_types.append(_apply_to.split("__:type:__")[-1][:-1])
            else:
                needed_types.append(_apply_to)
            if hasattr(step_dict.estimator, "get_needed_types"):
                needed_types.extend(step_dict.estimator.get_needed_types())

        needed_types = set(needed_types)
        applied_to_special = (needed_types == "continuous" or
                              needed_types == "target" or
                              ".*" in needed_types or
                              "*" in needed_types)
        for X_type in all_types:
            if X_type not in needed_types and not applied_to_special:
                warn(
                    f"{X_type} is provided but never used by a transformer. "
                    f"Used types are {needed_types}"
                )

        for needed_type in needed_types:
            if needed_type not in [
                    *all_types, "*", ".*", "target", "continuous"]:
                raise_error(
                    f"{needed_type} is not in the provided X_types={X_types}"
                )

        return [f"__:type:__{X_type}" for X_type in all_types]

    @staticmethod
    def _is_transfromer_step(step):
        if step in list_transformers():
            return True
        if hasattr(step, "fit") and hasattr(step, "transform"):
            return True
        return False

    @staticmethod
    def _is_model_step(step):
        if step in list_models():
            return True
        if hasattr(step, "fit") and hasattr(step, "predict"):
            return True
        return False

    @staticmethod
    def wrap_step(name, step, pattern):
        return ColumnTransformer(
            [(name, step, make_type_selector(pattern))],
            verbose_feature_names_out=False,
            remainder="passthrough",
        )

    @staticmethod
    def _ensure_apply_to(apply_to):
        return ensure_apply_to(apply_to)

    @staticmethod
    def get_estimator_from(name, problem_type, **kwargs):
        if name in list_transformers():
            return get_transformer(name, **kwargs)
        if name in list_models():
            return get_model(name, problem_type, **kwargs)
        raise_error(
            f"{name} is neither a registered transformer"
            "nor a registered model."
        )
