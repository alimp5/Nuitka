#     Copyright 2020, Kay Hayen, mailto:kay.hayen@gmail.com
#
#     Part of "Nuitka", an optimizing Python compiler that is compatible and
#     integrates with CPython, but also works on its own.
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.
#
""" Trace collection (also often still referred to as constraint collection).

At the core of value propagation there is the collection of constraints that
allow to propagate knowledge forward or not.

This is about collecting these constraints and to manage them.
"""

import contextlib

from nuitka import Tracing, Variables
from nuitka.__past__ import iterItems  # Python3 compatibility.
from nuitka.containers.oset import OrderedSet
from nuitka.importing.ImportCache import getImportedModuleByNameAndPath
from nuitka.ModuleRegistry import addUsedModule
from nuitka.nodes.NodeMakingHelpers import getComputationResult
from nuitka.nodes.shapes.BuiltinTypeShapes import tshape_dict
from nuitka.nodes.shapes.StandardShapes import tshape_uninit
from nuitka.tree.SourceReading import readSourceLine
from nuitka.utils.FileOperations import relpath
from nuitka.utils.InstanceCounters import counted_del, counted_init
from nuitka.utils.ModuleNames import ModuleName

from .ValueTraces import (
    ValueTraceAssign,
    ValueTraceDeleted,
    ValueTraceEscaped,
    ValueTraceInit,
    ValueTraceLoopComplete,
    ValueTraceLoopIncomplete,
    ValueTraceMerge,
    ValueTraceUninit,
    ValueTraceUnknown,
)

signalChange = None


class CollectionStartpointMixin(object):
    """Mixin to use in start points of collections.

    These are modules, functions, etc. typically entry points.
    """

    __slots__ = ()

    # Many things are traced

    def __init__(self):
        # Variable assignments performed in here, last issued number, only used
        # to determine the next number that should be used for a new assignment.
        self.variable_versions = {}

        # The full trace of a variable with a version for the function or module
        # this is.
        self.variable_traces = {}

        self.break_collections = None
        self.continue_collections = None
        self.return_collections = None
        self.exception_collections = None

        self.outline_functions = None

    def getLoopBreakCollections(self):
        return self.break_collections

    def onLoopBreak(self, collection=None):
        if collection is None:
            collection = self

        self.break_collections.append(
            TraceCollectionBranch(parent=collection, name="loop break")
        )

    def getLoopContinueCollections(self):
        return self.continue_collections

    def onLoopContinue(self, collection=None):
        if collection is None:
            collection = self

        self.continue_collections.append(
            TraceCollectionBranch(parent=collection, name="loop continue")
        )

    def onFunctionReturn(self, collection=None):
        if collection is None:
            collection = self

        if self.return_collections is not None:
            self.return_collections.append(
                TraceCollectionBranch(parent=collection, name="return")
            )

    def onExceptionRaiseExit(self, raisable_exceptions, collection=None):
        """Indicate to the trace collection what exceptions may have occurred.

        Args:
            raisable_exception: Currently ignored, one or more exceptions that
            could occur, e.g. "BaseException".
            collection: To pass the collection that will be the parent
        Notes:
            Currently this is unused. Passing "collection" as an argument, so
            we know the original collection to attach the branch to, is maybe
            not the most clever way to do this

            We also might want to specialize functions for specific exceptions,
            there is little point in providing BaseException as an argument in
            so many places.

            The actual storage of the exceptions that can occur is currently
            missing entirely. We just use this to detect "any exception" by
            not being empty.
        """

        # TODO: We might want to track per exception, pylint: disable=unused-argument

        if collection is None:
            collection = self

        if self.exception_collections is not None:
            self.exception_collections.append(
                TraceCollectionBranch(parent=collection, name="exception")
            )

    def getFunctionReturnCollections(self):
        return self.return_collections

    def getExceptionRaiseCollections(self):
        return self.exception_collections

    def hasVariableTrace(self, variable, version):
        return (variable, version) in self.variable_traces

    def getVariableTrace(self, variable, version):
        return self.variable_traces[(variable, version)]

    def getVariableTraces(self, variable):
        result = []

        for key, variable_trace in iterItems(self.variable_traces):
            candidate = key[0]

            if variable is candidate:
                result.append(variable_trace)

        return result

    def getVariableTracesAll(self):
        return self.variable_traces

    def addVariableTrace(self, variable, version, trace):
        key = variable, version

        assert key not in self.variable_traces, (key, self)
        self.variable_traces[key] = trace

    def addVariableMergeMultipleTrace(self, variable, traces):
        version = variable.allocateTargetNumber()

        trace_merge = ValueTraceMerge(traces)

        self.addVariableTrace(variable, version, trace_merge)

        return version

    def initVariableUnknown(self, variable):
        trace = ValueTraceUnknown(owner=self.owner, previous=None)

        self.addVariableTrace(variable=variable, version=0, trace=trace)

        return trace

    def _initVariableInit(self, variable):
        trace = ValueTraceInit(self.owner)

        self.addVariableTrace(variable=variable, version=0, trace=trace)

        return trace

    def _initVariableUninit(self, variable):
        trace = ValueTraceUninit(owner=self.owner, previous=None)

        self.addVariableTrace(variable=variable, version=0, trace=trace)

        return trace

    def updateVariablesFromCollection(self, old_collection, source_ref):
        Variables.updateVariablesFromCollection(old_collection, self, source_ref)

    @contextlib.contextmanager
    def makeAbortStackContext(
        self, catch_breaks, catch_continues, catch_returns, catch_exceptions
    ):
        if catch_breaks:
            old_break_collections = self.break_collections
            self.break_collections = []
        if catch_continues:
            old_continue_collections = self.continue_collections
            self.continue_collections = []
        if catch_returns:
            old_return_collections = self.return_collections
            self.return_collections = []
        if catch_exceptions:
            old_exception_collections = self.exception_collections
            self.exception_collections = []

        yield

        if catch_breaks:
            self.break_collections = old_break_collections
        if catch_continues:
            self.continue_collections = old_continue_collections
        if catch_returns:
            self.return_collections = old_return_collections
        if catch_exceptions:
            self.exception_collections = old_exception_collections

    def initVariable(self, variable):
        if variable.isParameterVariable():
            result = self._initVariableInit(variable)
        elif variable.isLocalVariable():
            result = self._initVariableUninit(variable)
        elif variable.isModuleVariable():
            result = self.initVariableUnknown(variable)
        elif variable.isTempVariable():
            result = self._initVariableUninit(variable)
        elif variable.isLocalsDictVariable():
            if variable.getOwner().getTypeShape() is tshape_dict:
                result = self._initVariableUninit(variable)
            else:
                result = self.initVariableUnknown(variable)
        else:
            assert False, variable

        return result

    def addOutlineFunction(self, outline):
        if self.outline_functions is None:
            self.outline_functions = [outline]
        else:
            self.outline_functions.append(outline)

    def getOutlineFunctions(self):
        return self.outline_functions

    def onLocalsDictEscaped(self, locals_scope):
        if locals_scope is not None:
            for variable in locals_scope.variables.values():
                self.markActiveVariableAsEscaped(variable)

        # TODO: Limit to the scope.
        for variable in self.getActiveVariables():
            if variable.isTempVariable() or variable.isModuleVariable():
                continue

            self.markActiveVariableAsEscaped(variable)


class TraceCollectionBase(object):
    """This contains for logic for maintaining active traces.

    They are kept for "variable" and versions.
    """

    __slots__ = ("owner", "parent", "name", "value_states", "variable_actives")

    __del__ = counted_del()

    @counted_init
    def __init__(self, owner, name, parent):
        self.owner = owner
        self.parent = parent
        self.name = name

        # Value state extra information per node.
        self.value_states = {}

        # Currently active values in the tracing.
        self.variable_actives = {}

    def __repr__(self):
        return "<%s for %s at 0x%x>" % (self.__class__.__name__, self.name, id(self))

    def getOwner(self):
        return self.owner

    def getVariableCurrentTrace(self, variable):
        """Get the current value trace associated to this variable

        It is also created on the fly if necessary. We create them
        lazy so to keep the tracing branches minimal where possible.
        """

        return self.getVariableTrace(
            variable=variable, version=self._getCurrentVariableVersion(variable)
        )

    def markCurrentVariableTrace(self, variable, version):
        self.variable_actives[variable] = version

    def _getCurrentVariableVersion(self, variable):
        try:
            return self.variable_actives[variable]
        except KeyError:
            # Initialize variables on the fly.
            if not self.hasVariableTrace(variable, 0):
                self.initVariable(variable)

            self.markCurrentVariableTrace(variable, 0)

            return self.variable_actives[variable]

    def getActiveVariables(self):
        return self.variable_actives.keys()

    def markActiveVariableAsEscaped(self, variable):
        current = self.getVariableCurrentTrace(variable=variable)

        if not current.isUnknownTrace():
            version = variable.allocateTargetNumber()

            self.addVariableTrace(
                variable=variable,
                version=version,
                trace=ValueTraceEscaped(owner=self.owner, previous=current),
            )

            self.markCurrentVariableTrace(variable, version)

    def markActiveVariableAsLoopMerge(
        self, loop_node, current, variable, shapes, incomplete
    ):
        if incomplete:
            result = ValueTraceLoopIncomplete(loop_node, current, shapes)
        else:
            # TODO: Empty is a missing optimization somewhere, but it also happens that
            # a variable is getting released in a loop.
            # assert shapes, (variable, current)

            if not shapes:
                shapes.add(tshape_uninit)

            result = ValueTraceLoopComplete(loop_node, current, shapes)

        version = variable.allocateTargetNumber()
        self.addVariableTrace(variable=variable, version=version, trace=result)

        self.markCurrentVariableTrace(variable, version)

        return result

    def markActiveVariablesAsUnknown(self):
        for variable in self.getActiveVariables():
            if variable.isTempVariable():
                continue

            self.markActiveVariableAsEscaped(variable)

    @staticmethod
    def signalChange(tags, source_ref, message):
        # This is monkey patched from another module. pylint: disable=I0021,not-callable
        signalChange(tags, source_ref, message)

    def onUsedModule(self, module_name, module_relpath):
        return self.parent.onUsedModule(module_name, module_relpath)

    @staticmethod
    def mustAlias(a, b):
        if a.isExpressionVariableRef() and b.isExpressionVariableRef():
            return a.getVariable() is b.getVariable()

        return False

    @staticmethod
    def mustNotAlias(a, b):
        # TODO: not yet really implemented
        if a.isExpressionConstantRef() and b.isExpressionConstantRef():
            if a.isMutable() or b.isMutable():
                return True

        return False

    def removeKnowledge(self, node):
        pass

    def onControlFlowEscape(self, node):
        # TODO: One day, we should trace which nodes exactly cause a variable
        # to be considered escaped, pylint: disable=unused-argument

        for variable in self.getActiveVariables():
            if variable.isModuleVariable():
                # print variable

                self.markActiveVariableAsEscaped(variable)

            elif variable.isLocalVariable():
                if variable.hasAccessesOutsideOf(self.owner) is not False:
                    self.markActiveVariableAsEscaped(variable)

    def removeAllKnowledge(self):
        self.markActiveVariablesAsUnknown()

    def getVariableTrace(self, variable, version):
        return self.parent.getVariableTrace(variable, version)

    def hasVariableTrace(self, variable, version):
        return self.parent.hasVariableTrace(variable, version)

    def addVariableTrace(self, variable, version, trace):
        self.parent.addVariableTrace(variable, version, trace)

    def addVariableMergeMultipleTrace(self, variable, traces):
        return self.parent.addVariableMergeMultipleTrace(variable, traces)

    def onVariableSet(self, variable, version, assign_node):
        variable_trace = ValueTraceAssign(
            owner=self.owner,
            assign_node=assign_node,
            previous=self.getVariableCurrentTrace(variable=variable),
        )

        self.addVariableTrace(variable=variable, version=version, trace=variable_trace)

        # Make references point to it.
        self.markCurrentVariableTrace(variable, version)

        return variable_trace

    def onVariableDel(self, variable, version, del_node):
        # Add a new trace, allocating a new version for the variable, and
        # remember the delete of the current
        old_trace = self.getVariableCurrentTrace(variable)

        variable_trace = ValueTraceDeleted(
            owner=self.owner, del_node=del_node, previous=old_trace
        )

        # Assign to not initialized again.
        self.addVariableTrace(variable=variable, version=version, trace=variable_trace)

        # Make references point to it.
        self.markCurrentVariableTrace(variable, version)

        return variable_trace

    def onLocalsUsage(self, locals_scope):
        self.onLocalsDictEscaped(locals_scope)

        result = []

        scope_locals_variables = locals_scope.getLocalsRelevantVariables()

        for variable in self.getActiveVariables():
            if variable.isLocalVariable() and variable in scope_locals_variables:
                variable_trace = self.getVariableCurrentTrace(variable)

                variable_trace.addNameUsage()
                result.append((variable, variable_trace))

        return result

    def onVariableContentEscapes(self, variable):
        # TODO: Make use of this information, when we trace lists, dicts, etc.
        # self.getVariableCurrentTrace(variable).onValueEscape()
        pass

    def onExpression(self, expression, allow_none=False):
        if expression is None and allow_none:
            return None

        assert expression.isExpression(), expression

        parent = expression.parent
        assert parent, expression

        # Now compute this expression, allowing it to replace itself with
        # something else as part of a local peep hole optimization.
        r = expression.computeExpressionRaw(trace_collection=self)
        assert type(r) is tuple, (expression, expression.getVisitableNodes(), r)

        new_node, change_tags, change_desc = r

        if change_tags is not None:
            # This is mostly for tracing and indication that a change occurred
            # and it may be interesting to look again.
            self.signalChange(change_tags, expression.getSourceReference(), change_desc)

        if new_node is not expression:
            parent.replaceChild(expression, new_node)

        return new_node

    def onStatement(self, statement):
        try:
            assert statement.isStatement(), statement

            new_statement, change_tags, change_desc = statement.computeStatement(self)

            # print new_statement, change_tags, change_desc
            if new_statement is not statement:
                self.signalChange(
                    change_tags, statement.getSourceReference(), change_desc
                )

            return new_statement
        except Exception:
            Tracing.printError(
                "Problem with statement at %s:\n-> %s"
                % (
                    statement.source_ref.getAsString(),
                    readSourceLine(statement.source_ref),
                )
            )
            raise

    def computedStatementResult(self, statement, change_tags, change_desc):
        """Make sure the replacement statement is computed.

        Use this when a replacement expression needs to be seen by the trace
        collection and be computed, without causing any duplication, but where
        otherwise there would be loss of annotated effects.

        This may e.g. be true for nodes that need an initial run to know their
        exception result and type shape.
        """
        # Need to compute the replacement still.
        new_statement = statement.computeStatement(self)

        if new_statement[0] is not statement:
            # Signal intermediate result as well.
            self.signalChange(change_tags, statement.getSourceReference(), change_desc)

            return new_statement
        else:
            return statement, change_tags, change_desc

    def computedExpressionResult(self, expression, change_tags, change_desc):
        """Make sure the replacement expression is computed.

        Use this when a replacement expression needs to be seen by the trace
        collection and be computed, without causing any duplication, but where
        otherwise there would be loss of annotated effects.

        This may e.g. be true for nodes that need an initial run to know their
        exception result and type shape.
        """

        # Need to compute the replacement still.
        new_expression = expression.computeExpression(self)

        if new_expression[0] is not expression:
            # Signal intermediate result as well.
            self.signalChange(change_tags, expression.getSourceReference(), change_desc)

            return new_expression
        else:
            return expression, change_tags, change_desc

    def mergeBranches(self, collection_yes, collection_no):
        """Merge two alternative branches into this trace.

        This is mostly for merging conditional branches, or other ways
        of having alternative control flow. This deals with up to two
        alternative branches to both change this collection.
        """

        # Many branches due to inlining the actual merge and preparing it
        # pylint: disable=too-many-branches

        if collection_yes is None:
            if collection_no is not None:
                # Handle one branch case, we need to merge versions backwards as
                # they may make themselves obsolete.
                collection1 = self
                collection2 = collection_no
            else:
                # Refuse to do stupid work
                return
        elif collection_no is None:
            # Handle one branch case, we need to merge versions backwards as
            # they may make themselves obsolete.
            collection1 = self
            collection2 = collection_yes
        else:
            # Handle two branch case, they may or may not do the same things.
            collection1 = collection_yes
            collection2 = collection_no

        variable_versions = {}

        for variable, version in iterItems(collection1.variable_actives):
            variable_versions[variable] = version

        for variable, version in iterItems(collection2.variable_actives):
            if variable not in variable_versions:
                variable_versions[variable] = 0, version
            else:
                other = variable_versions[variable]

                if other != version:
                    variable_versions[variable] = other, version
                else:
                    variable_versions[variable] = other

        for variable in variable_versions:
            if variable not in collection2.variable_actives:
                variable_versions[variable] = variable_versions[variable], 0

        self.variable_actives = {}

        for variable, versions in iterItems(variable_versions):
            if type(versions) is tuple:
                version = self.addVariableMergeMultipleTrace(
                    variable=variable,
                    traces=(
                        self.getVariableTrace(variable, versions[0]),
                        self.getVariableTrace(variable, versions[1]),
                    ),
                )
            else:
                version = versions

            self.markCurrentVariableTrace(variable, version)

    def mergeMultipleBranches(self, collections):
        assert collections

        # Optimize for length 1, which is trivial merge and needs not a
        # lot of work.
        if len(collections) == 1:
            self.replaceBranch(collections[0])
            return None

        variable_versions = {}

        for collection in collections:
            for variable, version in iterItems(collection.variable_actives):
                if variable not in variable_versions:
                    variable_versions[variable] = OrderedSet((version,))
                else:
                    variable_versions[variable].add(version)

        for collection in collections:
            for variable, versions in iterItems(variable_versions):
                if variable not in collection.variable_actives:
                    versions.add(0)

        self.variable_actives = {}

        for variable, versions in iterItems(variable_versions):
            if len(versions) == 1:
                (version,) = versions
            else:
                version = self.addVariableMergeMultipleTrace(
                    variable=variable,
                    traces=tuple(
                        self.getVariableTrace(variable, version) for version in versions
                    ),
                )

            self.markCurrentVariableTrace(variable, version)

    def replaceBranch(self, collection_replace):
        self.variable_actives.update(collection_replace.variable_actives)
        collection_replace.variable_actives = None

    def onLoopBreak(self, collection=None):
        if collection is None:
            collection = self

        return self.parent.onLoopBreak(collection)

    def onLoopContinue(self, collection=None):
        if collection is None:
            collection = self

        return self.parent.onLoopContinue(collection)

    def onFunctionReturn(self, collection=None):
        if collection is None:
            collection = self

        return self.parent.onFunctionReturn(collection)

    def onExceptionRaiseExit(self, raisable_exceptions, collection=None):
        if collection is None:
            collection = self

        return self.parent.onExceptionRaiseExit(raisable_exceptions, collection)

    def getLoopBreakCollections(self):
        return self.parent.getLoopBreakCollections()

    def getLoopContinueCollections(self):
        return self.parent.getLoopContinueCollections()

    def getFunctionReturnCollections(self):
        return self.parent.getFunctionReturnCollections()

    def getExceptionRaiseCollections(self):
        return self.parent.getExceptionRaiseCollections()

    def makeAbortStackContext(
        self, catch_breaks, catch_continues, catch_returns, catch_exceptions
    ):
        return self.parent.makeAbortStackContext(
            catch_breaks=catch_breaks,
            catch_continues=catch_continues,
            catch_returns=catch_returns,
            catch_exceptions=catch_exceptions,
        )

    def onLocalsDictEscaped(self, locals_scope):
        self.parent.onLocalsDictEscaped(locals_scope)

    def getCompileTimeComputationResult(self, node, computation, description):
        new_node, change_tags, message = getComputationResult(
            node=node, computation=computation, description=description
        )

        if change_tags == "new_raise":
            self.onExceptionRaiseExit(BaseException)

        return new_node, change_tags, message

    def getIteratorNextCount(self, iter_node):
        return self.value_states.get(iter_node)

    def initIteratorValue(self, iter_node):
        # TODO: More complex state information will be needed eventually.
        self.value_states[iter_node] = 0

    def onIteratorNext(self, iter_node):
        if iter_node in self.value_states:
            self.value_states[iter_node] += 1

    def resetValueStates(self):
        for key in self.value_states:
            self.value_states[key] = None

    def addOutlineFunction(self, outline):
        self.parent.addOutlineFunction(outline)


class TraceCollectionBranch(TraceCollectionBase):
    __slots__ = ()

    def __init__(self, name, parent):
        TraceCollectionBase.__init__(self, owner=parent.owner, name=name, parent=parent)

        # Detach from others
        self.variable_actives = dict(parent.variable_actives)

    def computeBranch(self, branch):
        if branch.isStatementsSequence():
            result = branch.computeStatementsSequence(trace_collection=self)

            if result is not branch:
                branch.parent.replaceChild(branch, result)
        else:
            self.onExpression(expression=branch)

    def initVariable(self, variable):
        variable_trace = self.parent.initVariable(variable)

        self.variable_actives[variable] = 0

        return variable_trace

    def onLocalsDictEscaped(self, locals_scope):
        return self.parent.onLocalsDictEscaped(locals_scope)

    def dumpTraces(self):
        Tracing.printSeparator()
        self.parent.dumpTraces()
        Tracing.printSeparator()

    def dumpActiveTraces(self):
        Tracing.printSeparator()
        Tracing.printLine("Active are:")
        for variable, _version in sorted(self.variable_actives.iteritems()):
            self.getVariableCurrentTrace(variable).dump()

        Tracing.printSeparator()


class TraceCollectionFunction(CollectionStartpointMixin, TraceCollectionBase):
    __slots__ = (
        "variable_versions",
        "variable_traces",
        "break_collections",
        "continue_collections",
        "return_collections",
        "exception_collections",
        "outline_functions",
    )

    def __init__(self, parent, function_body):
        assert (
            function_body.isExpressionFunctionBody()
            or function_body.isExpressionGeneratorObjectBody()
            or function_body.isExpressionCoroutineObjectBody()
            or function_body.isExpressionAsyncgenObjectBody()
        ), function_body

        CollectionStartpointMixin.__init__(self)

        TraceCollectionBase.__init__(
            self,
            owner=function_body,
            name="collection_" + function_body.getCodeName(),
            parent=parent,
        )

        if function_body.isExpressionFunctionBody():
            for parameter_variable in function_body.getParameters().getAllVariables():
                self._initVariableInit(parameter_variable)
                self.variable_actives[parameter_variable] = 0

        for closure_variable in function_body.getClosureVariables():
            self.initVariableUnknown(closure_variable)
            self.variable_actives[closure_variable] = 0

        # TODO: Have special function type for exec functions stuff.
        locals_scope = function_body.getLocalsScope()

        if locals_scope is not None:
            if not locals_scope.isMarkedForPropagation():
                for locals_dict_variable in locals_scope.variables.values():
                    self._initVariableUninit(locals_dict_variable)
            else:
                function_body.locals_scope = None


class TraceCollectionModule(CollectionStartpointMixin, TraceCollectionBase):
    __slots__ = (
        "variable_versions",
        "variable_traces",
        "break_collections",
        "continue_collections",
        "return_collections",
        "exception_collections",
        "outline_functions",
    )

    def __init__(self, module):
        assert module.isCompiledPythonModule(), module

        CollectionStartpointMixin.__init__(self)

        TraceCollectionBase.__init__(
            self, owner=module, name="module:" + module.getFullName(), parent=None
        )

    def onUsedModule(self, module_name, module_relpath):
        assert type(module_name) is ModuleName, module_name

        # TODO: Make users provide this through a method that has already
        # done this.
        module_relpath = relpath(module_relpath)

        self.owner.addUsedModule((module_name, module_relpath))

        module = getImportedModuleByNameAndPath(module_name, module_relpath)
        addUsedModule(module)
