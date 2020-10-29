import numpy as np
from sklearn import svm
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import cross_val_score, RepeatedKFold
from sklearn.preprocessing import LabelBinarizer
from seaborn import load_dataset
from julearn import run_cross_validation
from julearn.transformers import TargetTransfromerWrapper


def test_simple_binary():
    """Test simple binary classification"""
    df_iris = load_dataset('iris')

    # keep only two species
    df_iris = df_iris[df_iris['species'].isin(['setosa', 'virginica'])]
    X = ['sepal_length', 'sepal_width', 'petal_length']
    y = 'species'

    sk_X = df_iris[X].values
    sk_y = df_iris[y].values

    scorers = ['accuracy', 'balanced_accuracy']
    for scoring in scorers:
        actual = run_cross_validation(X=X, y=y, data=df_iris, model='svm',
                                      seed=42, scoring=scoring)

        # Now do the same with scikit-learn
        clf = make_pipeline(StandardScaler(), svm.SVC())

        np.random.seed(42)
        cv = RepeatedKFold(n_splits=5, n_repeats=5)
        expected = cross_val_score(clf, sk_X, sk_y, cv=cv, scoring=scoring)

        assert len(actual) == len(expected)
        assert all([a == b for a, b in zip(actual, expected)])

    # now let's try target-dependent scores
    scorers = ['recall', 'precision', 'f1']
    t_sk_y = (sk_y == 'setosa').astype(np.int)
    for scoring in scorers:
        actual = run_cross_validation(X=X, y=y, data=df_iris, model='svm',
                                      seed=42, scoring=scoring,
                                      pos_labels='setosa')

        # Now do the same with scikit-learn
        clf = make_pipeline(StandardScaler(), svm.SVC())

        np.random.seed(42)
        cv = RepeatedKFold(n_splits=5, n_repeats=5)

        expected = cross_val_score(clf, sk_X, t_sk_y, cv=cv, scoring=scoring)

        assert len(actual) == len(expected)
        assert all([a == b for a, b in zip(actual, expected)])

    # now let's try proba-dependent scores
    scorers = ['roc_auc']
    t_sk_y = (sk_y == 'setosa').astype(np.int)
    for scoring in scorers:
        model = svm.SVC(probability=True)
        actual = run_cross_validation(X=X, y=y, data=df_iris, model=model,
                                      seed=42, scoring=scoring,
                                      pos_labels='setosa')

        # Now do the same with scikit-learn
        clf = make_pipeline(StandardScaler(), svm.SVC())

        np.random.seed(42)
        cv = RepeatedKFold(n_splits=5, n_repeats=5)

        expected = cross_val_score(clf, sk_X, t_sk_y, cv=cv, scoring=scoring)

        assert len(actual) == len(expected)
        assert all([a == b for a, b in zip(actual, expected)])


def test_scoring_y_transformer():
    df_iris = load_dataset('iris')

    # keep only two species
    df_iris = df_iris[df_iris['species'].isin(['setosa', 'virginica'])]
    X = ['sepal_length', 'sepal_width', 'petal_length']
    y = 'species'

    sk_X = df_iris[X].values
    sk_y = df_iris[y].values

    scorers = ['accuracy', 'balanced_accuracy']
    for scoring in scorers:
        y_transformer = LabelBinarizer()
        actual = run_cross_validation(
            X=X, y=y, data=df_iris, model='svm', preprocess_y=y_transformer,
            seed=42, scoring=scoring)

        # Now do the same with scikit-learn
        clf = make_pipeline(StandardScaler(), svm.SVC())

        np.random.seed(42)
        cv = RepeatedKFold(n_splits=5, n_repeats=5)
        expected = cross_val_score(clf, sk_X, sk_y, cv=cv, scoring=scoring)

        assert len(actual) == len(expected)
        assert all([a == b for a, b in zip(actual, expected)])
